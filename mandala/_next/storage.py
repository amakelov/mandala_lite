from .common_imports import *
from tqdm import tqdm
import prettytable
import datetime
from .model import *
import sqlite3
from .model import __make_list__, __get_list_item__, __make_dict__, __get_dict_value__, _Ignore, _NewArgDefault
from .utils import dataframe_to_prettytable, parse_returns
from .viz import _get_colorized_diff
from .deps.versioner import Versioner, CodeState
from .deps.utils import get_dep_key_from_func, extract_func_obj
from .deps.tracers import DecTracer, SysTracer, TracerABC

from .storage_utils import (
    DBAdapter,
    InMemCallStorage,
    SQLiteCallStorage,
    CachedDictStorage,
    SQLiteDictStorage,
    CachedCallStorage,
    transaction
)


class Storage:
    def __init__(self, db_path: str = ":memory:", 
                 deps_path: Optional[Union[str, Path]] = None,
                 tracer_impl: Optional[type] = None,
                 deps_package: Optional[str] = None,
                 ):
        self.db = DBAdapter(db_path=db_path)

        self.call_storage = SQLiteCallStorage(db=self.db, table_name="calls")
        self.calls = CachedCallStorage(persistent=self.call_storage)
        self.call_cache = self.calls.cache

        self.atoms = CachedDictStorage(
            persistent=SQLiteDictStorage(self.db, table="atoms")
        )
        self.shapes = CachedDictStorage(
            persistent=SQLiteDictStorage(self.db, table="shapes")
        )
        self.ops = CachedDictStorage(
            persistent=SQLiteDictStorage(self.db, table="ops")
        )

        self.sources = CachedDictStorage(
            persistent=SQLiteDictStorage(self.db, table="sources")
        )
        if not self.sources.exists(key='versioner'):
            current_versioner = None
        else:
            current_versioner = self.sources['versioner']

        # self.version_adapter = VersionAdapter(rel_adapter=self.rel_adapter)
        if deps_path is not None:
            deps_path = (
                Path(deps_path).absolute().resolve()
                if deps_path != "__main__"
                else "__main__"
            )
            roots = [] if deps_path == "__main__" else [deps_path]
            self._versioned = True
            if current_versioner is not None:
                if current_versioner.paths != roots:
                    raise ValueError(
                        f"Found existing versioner with roots {current_versioner.paths}, but "
                        f"was asked to use {roots}"
                    )
            else:
                versioner = Versioner(
                    paths=roots,
                    TracerCls=DecTracer if tracer_impl is None else tracer_impl,
                    strict=True,
                    track_methods=True,
                    package_name=deps_package,
                )
                # self.version_adapter.dump_state(state=versioner)
                self.sources["versioner"] = versioner
        else:
            self._versioned = False
        
        self.cached_versioner = None
        self.code_state = None
        self.suspended_trace_obj = None

    
    def conn(self) -> sqlite3.Connection:
        return self.db.conn()

    def vacuum(self):
        with self.conn() as conn:
            conn.execute("VACUUM")

    ############################################################################
    ### managing the caches
    ############################################################################
    def cache_info(self) -> str:
        """
        Display information about the contents of the cache in a pretty table.

        TODO: make a verbose version w/ a breakdown by op, and some data on
        memory usage.
        """
        df = pd.DataFrame({
            'present': [len(self.atoms.cache), len(self.shapes.cache), len(self.ops.cache), len(self.calls.cache)],
            'dirty': [len(self.atoms.dirty_keys), len(self.shapes.dirty_keys), len(self.ops.dirty_keys), len(self.calls.dirty_hids)]
        }, index=['atoms', 'shapes', 'ops', 'calls']).reset_index().rename(columns={'index': 'cache'})
        print(dataframe_to_prettytable(df))

    def preload_calls(self):
        df = self.call_storage.get_df()
        self.call_cache.df = df
        self.call_cache.call_hids = set(df.index.levels[0].unique())
    
    def preload_shapes(self):
        self.shapes.cache = self.shapes.persistent.load_all()
    
    def preload_ops(self):
        self.ops.cache = self.ops.persistent.load_all()

    def preload_atoms(self):
        self.atoms.cache = self.atoms.persistent.load_all()
    
    def preload(self, lazy: bool = True):
        self.preload_calls()
        self.preload_shapes()
        self.preload_ops()
        if not lazy:
            self.preload_atoms()

    def commit(self):
        with self.conn() as conn:
            self.atoms.commit(conn=conn)
            self.shapes.commit(conn=conn)
            self.ops.commit(conn=conn)
            self.calls.commit(conn=conn)


    def __repr__(self):
        # summarize cache sizes
        cache_sizes = {
            "atoms": len(self.atoms.cache),
            "shapes": len(self.shapes.cache),
            "ops": len(self.ops.cache),
            "calls": len(self.calls.cache),
        }
        return f"Storage(db_path={self.db_path}), cache contents:\n" + "\n".join(
            [f"  {k}: {v}" for k, v in cache_sizes.items()]
        )

    def in_context(self) -> bool:
        return Context.current_context is not None

    def _tables(self) -> List[str]:
        with self.conn() as conn:
            res = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table';"
            ).fetchall()
        return [row[0] for row in res]

    ############################################################################
    ### refs interface
    ############################################################################
    def save_ref(self, ref: Ref):
        """
        NOTE: the given ref may not be in memory, but may still be a new history
        ID, if there was previously another ref with the same content ID but
        different history ID.
        """
        if ref.hid in self.shapes:  # ensure idempotence
            return
        if isinstance(ref, AtomRef):
            if ref.in_memory: #! ONLY save the atom if it is in memory
                self.atoms[ref.cid] = serialize(ref.obj)
            self.shapes[ref.hid] = ref.detached()
        elif isinstance(ref, ListRef):
            self.shapes[ref.hid] = ref.shape()
            for i, elt in enumerate(ref):
                self.save_ref(elt)
        elif isinstance(ref, DictRef):
            self.shapes[ref.hid] = ref.shape()
            for k, v in ref.items():
                self.save_ref(v)
                # self.save_ref(k)
        else:
            raise NotImplementedError

    def load_ref(self, hid: str, lazy: bool = False) -> Ref:
        shape = self.shapes[hid]
        if isinstance(shape, AtomRef):
            if lazy:
                return shape.shallow_copy()
            else:
                return shape.attached(obj=deserialize(self.atoms[shape.cid]))
        elif isinstance(shape, ListRef):
            obj = []
            for i, elt in enumerate(shape):
                obj.append(self.load_ref(elt.hid, lazy=lazy))
            return shape.attached(obj=shape.obj)
        elif isinstance(shape, DictRef):
            obj = {}
            for k, v in shape.items():
                obj[k] = self.load_ref(v.hid, lazy=lazy)
            return shape.attached(obj=obj)
        else:
            raise NotImplementedError

    def _drop_ref_hid(self, hid: str, verify: bool = False):
        """
        Internal only function to drop a ref by its hid when it is not connected
        to any calls.
        """
        if verify:
            assert not self.call_storage.exists_ref_hid(hid)
        self.shapes.drop(hid)

    def _drop_ref(self, cid: str, verify: bool = False):
        """
        Internal only function to drop a ref by its cid when it is not connected
        to any calls and is not in the `shapes` table.
        """
        if verify:
            assert cid not in [shape.cid for shape in self.shapes.persistent.values()]
        self.atoms.drop(cid)

    def cleanup_refs(self):
        """
        Remove all refs that are not connected to any calls.
        """
        ### first, remove hids that are not connected to any calls
        orphans = self.get_orphans()
        logger.info(f"Cleaning up {len(orphans)} orphaned refs.")
        for hid in orphans:
            self._drop_ref_hid(hid)
        ### next, remove cids that are not connected to any refs
        unreferenced_cids = self.get_unreferenced_cids()
        logger.info(f"Cleaning up {len(unreferenced_cids)} unreferenced cids.")
        for cid in unreferenced_cids:
            self._drop_ref(cid)

    ############################################################################
    ### calls interface
    ############################################################################
    def exists_call(self, hid: str) -> bool:
        return self.calls.exists(hid)

    def save_call(self, call: Call):
        """
        Save a *new* call to the storage. All inputs/outputs must be present.

        If the op has not been seen before, it will be saved as well.
        """
        if self.calls.exists(call.hid):
            return
        if not self.ops.exists(key=call.op.name):
            # save the op
            logging.info(f"Caching new op {call.op.name}.")
            self.ops[call.op.name] = call.op.detached()
        # (convert the iterator to a list to avoid double iteration)
        io_refs = list(itertools.chain(call.inputs.values(), call.outputs.values()))
        for v in io_refs:
            self.save_ref(v)
        self.calls.save(call)
    
    def mget_call(self, hids: List[str], lazy: bool) -> List[Call]:

        def split_list(lst: List[Any], mask: List[bool]) -> Tuple[List[Any], List[Any]]:
            return [x for x, m in zip(lst, mask) if m], [x for x, m in zip(lst, mask) if not m]
        
        def merge_lists(list_true: List[Any], list_false: List[Any], mask: List[bool]) -> List[Any]:
            res = []
            i, j = 0, 0
            for m in mask:
                if m:
                    res.append(list_true[i])
                    i += 1
                else:
                    res.append(list_false[j])
                    j += 1
            return res

        mask = [self.call_cache.exists(hid) for hid in hids]
        cache_part, db_part = split_list(hids, mask)
        # cache_datas = [self.call_cache.get_data(hid) for hid in tqdm(cache_part)]
        cache_datas = self.call_cache.mget_data(call_hids=cache_part)
        db_datas = self.call_storage.mget_data(call_hids=db_part)
        call_datas = merge_lists(cache_datas, db_datas, mask)

        calls = []
        for call_data in call_datas:
            calls.append(self._get_call_from_data(call_data, lazy=lazy))
        return calls
    
    def _get_call_from_data(self, call_data: Dict[str, Any], lazy: bool) -> Call:
        op_name = call_data["op_name"]
        call = Call(
            op=self.ops[op_name],
            cid=call_data["cid"],
            hid=call_data["hid"],
            inputs={
                k: self.load_ref(v, lazy=lazy)
                for k, v in call_data["input_hids"].items()
            },
            outputs={
                k: self.load_ref(v, lazy=lazy)
                for k, v in call_data["output_hids"].items()
            },
            semantic_version=call_data.get("semantic_version", None),
            content_version=call_data.get("content_version", None),
        )
        return call

    def get_call(self, hid: str, lazy: bool) -> Call:
        return self.mget_call([hid], lazy=lazy)[0]

    def drop_calls(self, calls_or_hids: Union[Iterable[str], Iterable[Call]], delete_dependents: bool):
        """
        Remove the calls with the given HIDs (and optionally their dependents)
        from the storage *and* the cache. 

        Removing from the cache is necessary to ensure that future lookups don't
        hit a false positive.
        """
        # figure out what we're given    
        if all(isinstance(c, Call) for c in calls_or_hids):
            hids = [c.hid for c in calls_or_hids]
        else:
            hids = list(calls_or_hids)
        hids = set(hids)
        if delete_dependents:
            _, dependent_call_hids = self.call_storage.get_dependents(
                ref_hids=set(), call_hids=hids
            )
            hids |= dependent_call_hids
        num_dropped_cache = 0
        num_dropped_persistent = 0
        for hid in hids:
            if self.call_cache.exists(hid):
                self.call_cache.drop(hid)
                num_dropped_cache += 1
        for hid in hids:
            if self.call_storage.exists(hid):
                self.call_storage.drop(hid)
                num_dropped_persistent += 1
        logger.info(f"Dropped {num_dropped_persistent} calls (and {num_dropped_cache} from cache).")

    ############################################################################
    ### provenance queries
    ############################################################################
    def get_creators(self, ref_hids: Iterable[str]) -> List[Call]:
        if self.in_context():
            raise NotImplementedError("Method not supported while in a context.")
        call_hids = self.call_storage.get_creator_hids(ref_hids)
        # return [self.get_call(call_hid, lazy=True) for call_hid in call_hids]
        return self.mget_call(hids=call_hids, lazy=True)

    def get_consumers(self, ref_hids: Iterable[str]) -> List[Call]:
        if self.in_context():
            raise NotImplementedError("Method not supported while in a context.")
        call_hids = self.call_storage.get_consumer_hids(ref_hids)
        # return [self.get_call(call_hid, lazy=True) for call_hid in call_hids]
        return self.mget_call(hids=call_hids, lazy=True)

    def get_orphans(self) -> Set[str]:
        """
        Return the HIDs of the refs not connected to any calls.
        """
        if self.in_context():
            raise NotImplementedError("Method not supported while in a context.")
        all_hids = set(self.shapes.persistent.keys())
        df = self.call_storage.get_df()
        hids_in_calls = df["ref_history_id"].unique()
        return all_hids - set(hids_in_calls)

    def get_unreferenced_cids(self) -> Set[str]:
        """
        Return the CIDs of the refs that don't appear in any calls or in the `shapes` table.
        """
        if self.in_context():
            raise NotImplementedError("Method not supported while in a context.")
        all_cids = set(self.atoms.persistent.keys())
        df = self.call_storage.get_df()
        cids_in_calls = df["ref_content_id"].unique()
        cids_in_shapes = [shape.cid for shape in self.shapes.persistent.values()]
        return (all_cids - set(cids_in_calls)) - set(cids_in_shapes)

    ############################################################################
    ###
    ############################################################################
    def _unwrap_atom(self, obj: Any) -> Any:
        assert isinstance(obj, AtomRef)
        if not obj.in_memory:
            ref = self.load_ref(hid=obj.hid, lazy=False)
            return ref.obj
        else:
            return obj.obj

    def _attach_atom(self, obj: AtomRef) -> AtomRef:
        assert isinstance(obj, AtomRef)
        if not obj.in_memory:
            obj.obj = deserialize(self.atoms[obj.cid])
            obj.in_memory = True
        return obj

    def unwrap(self, obj: Any) -> Any:
        return recurse_on_ref_collections(self._unwrap_atom, obj)

    def attach(self, obj: T) -> T:
        return recurse_on_ref_collections(self._attach_atom, obj)

    def get_struct_builder(self, tp: Type) -> Op:
        # the builtin op that will construct instances of this type
        if isinstance(tp, ListType):
            return __make_list__
        elif isinstance(tp, DictType):
            return __make_dict__
        else:
            raise NotImplementedError

    def get_struct_inputs(self, tp: Type, val: Any) -> Dict[str, Any]:
        # given a value to be interpreted as an instance of a struct,
        # return the inputs that would be passed to the struct builder
        if isinstance(tp, ListType):
            return {f"elts_{i}": elt for i, elt in enumerate(val)}
        elif isinstance(tp, DictType):
            # the keys must be strings
            assert all(isinstance(k, str) for k in val.keys())
            res = val
            # sorted_keys = sorted(val.keys())
            # res = {}
            # for i, k in enumerate(sorted_keys):
            #     res[f'key_{i}'] = k
            #     res[f'value_{i}'] = val[k]
            return res
        else:
            raise NotImplementedError

    def get_struct_tps(
        self, tp: Type, struct_inputs: Dict[str, Any]
    ) -> Dict[str, Type]:
        """
        Given the inputs to a struct builder, return the annotations that would
        be passed to the struct builder.
        """
        if isinstance(tp, ListType):
            return {f"elts_{i}": tp.elt for i in range(len(struct_inputs))}
        elif isinstance(tp, DictType):
            result = {}
            for input_name in struct_inputs.keys():
                result[input_name] = tp.val
                # if input_name.startswith("key_"):
                #     i = int(input_name.split("_")[-1])
                #     result[f"key_{i}"] = tp.key
                # elif input_name.startswith("value_"):
                #     i = int(input_name.split("_")[-1])
                #     result[f"value_{i}"] = tp.val
                # else:
                #     raise ValueError(f"Invalid input name {input_name}")
            return result
        else:
            raise NotImplementedError

    def construct(self, tp: Type, val: Any) -> Tuple[Ref, List[Call]]:
        if isinstance(val, Ref):
            return val, []
        if isinstance(tp, AtomType):
            return wrap_atom(val, None), []
        struct_builder = self.get_struct_builder(tp)
        struct_inputs = self.get_struct_inputs(tp=tp, val=val)
        struct_tps = self.get_struct_tps(tp=tp, struct_inputs=struct_inputs)
        res, main_call, calls = self.call_internal(
            op=struct_builder, storage_inputs=struct_inputs, storage_tps=struct_tps
        )
        calls.append(main_call)
        output_ref = main_call.outputs[list(main_call.outputs.keys())[0]]
        return output_ref, calls

    def destruct(self, ref: Ref, tp: Type) -> Tuple[Ref, List[Call]]:
        """
        Given a value w/ correct hid but possibly incorrect internal hids,
        destructure it, and return a value with correct internal hids and the
        calls that were made to destructure the value.
        """
        destr_calls = []
        if isinstance(ref, AtomRef):
            return ref, destr_calls
        elif isinstance(ref, ListRef):
            assert isinstance(ref, ListRef)
            assert isinstance(tp, ListType)
            new_elts = []
            for i, elt in enumerate(ref):
                getitem_dict, item_call, _ = self.call_internal(
                    op=__get_list_item__,
                    storage_inputs={"obj": ref, "attr": i},
                    storage_tps={"obj": tp, "attr": AtomType()},
                )
                new_elt = getitem_dict["output_0"]
                destr_calls.append(item_call)
                new_elt, elt_subcalls = self.destruct(new_elt, tp=tp.elt)
                new_elts.append(new_elt)
                destr_calls.extend(elt_subcalls)
            res = ListRef(cid=ref.cid, hid=ref.hid, in_memory=True, obj=new_elts)
            return res, destr_calls
        elif isinstance(ref, DictRef):
            assert isinstance(ref, DictRef)
            assert isinstance(tp, DictType)
            new_items = {}
            for k, v in ref.items():
                getvalue_dict, value_call, _ = self.call_internal(
                    op=__get_dict_value__,
                    storage_inputs={"obj": ref, "key": k},
                    storage_tps={"obj": tp, "key": tp.key},
                )
                new_v = getvalue_dict["output_0"]
                destr_calls.append(value_call)
                new_v, v_subcalls = self.destruct(new_v, tp=tp.val)
                new_items[k] = new_v
                destr_calls.extend(v_subcalls)
            res = DictRef(cid=ref.cid, hid=ref.hid, in_memory=True, obj=new_items)
            return res, destr_calls
        else:
            raise NotImplementedError
    
    def lookup_call(
        self,
        op: Op,
        inputs: Dict[str, Ref],
        pre_call_uid: Optional[str] = None,
        code_state: Optional[CodeState] = None,
        versioner: Optional[Versioner] = None,
    ) -> Optional[Call]:
        """
        Look up the call to the given op. If the storage is versioned, the
        current code state is used to resolve the right version of the function
        to use.
        """
        if not self.versioned:
            semantic_version = None
        else:
            assert code_state is not None and versioner is not None and pre_call_uid is not None
            component = get_dep_key_from_func(func=op.f)
            lookup_outcome = versioner.lookup_call(
                component=component, pre_call_uid=pre_call_uid, code_state=code_state
            )
            if lookup_outcome is None:
                logging.debug(f"Could not find a version for {op.name}.")
                return
            else:
                _, semantic_version = lookup_outcome
        ### first, look up by history ID
        call_hid = op.get_call_history_id(
            inputs=inputs,
            semantic_version=semantic_version,
        )
        if self.call_cache.exists(hid=call_hid):
            call_data = self.call_cache.get_data(call_history_id=call_hid)
            return self._get_call_from_data(call_data, lazy=True)
        ### if this fails, look up by content ID, and apply the correct history IDs
        call_cid = op.get_call_content_id(
            inputs=inputs, semantic_version=semantic_version
        )
        if self.call_cache.exists_content(cid=call_cid):
            call_data = self.call_cache.get_data_content(cid=call_cid)
            call_prototype = self._get_call_from_data(call_data, lazy=True)
            #!!! set the hids here on both the call and the outputs
            call_prototype.hid = call_hid
            output_names_identity = {k: k for k in call_data['output_hids'].keys()}
            output_history_ids = op.get_output_history_ids(call_history_id=call_hid, output_names=list(op.get_ordered_outputs(output_dict=output_names_identity)))
            for k, v in call_prototype.outputs.items():
                v.hid = output_history_ids[k]
            return call_prototype
        logging.debug(f"Could not find a call to {op.name} with hid {call_hid} or cid {call_cid}.")
        return None
    
    def get_defaults(self, f: Callable) -> Dict[str, Any]:
        return {
            k: v.default
            for k, v in inspect.signature(f).parameters.items()
            if v.default is not inspect.Parameter.empty
        }
    
    def preprocess_args_kwargs(self,
                               f: Callable,
                               args: Tuple[Any, ...],
                               kwargs: Dict[str, Any]):
        """
        Find which inputs should be ignored by the storage, and replace them
        with their underlying objects.
        """
        sig = inspect.signature(f)
        defaults = self.get_defaults(f)
        bound_args = sig.bind(*args, **kwargs)
        bound_args.apply_defaults()
        ignored_inputs = set()
        for k, v in bound_args.arguments.items():
            if isinstance(v, _Ignore):
                ignored_inputs.add(k)
        # now, check for defaults we should ignore
        for k, v in defaults.items():
            given_value = bound_args.arguments[v]
            if isinstance(given_value, _Ignore):
                ignored_inputs.add(k)
            elif isinstance(v, _NewArgDefault):
                if isinstance(given_value, Ref):
                    if self.unwrap(given_value) == v.value:
                        ignored_inputs.add(k)
                else:
                    if given_value == v.value:
                        ignored_inputs.add(k)
    
    def parse_args(self, sig: inspect.Signature, args, kwargs, apply_defaults: bool) -> Tuple[inspect.BoundArguments, Dict[str, Any], Dict[str, Any]]:
        """
        Given the inputs passed to an @op call (could be wrapped or unwrapped),
        figure out the inputs we should pass to storage functions, their type
        annotations, and the inputs we should pass to function calls.
         
        Handles ignored values and/or new arg defaults that should be ignored.
        """
        var_positional = [p for p in sig.parameters.values() if p.kind == p.VAR_POSITIONAL]
        var_positional = var_positional[0] if len(var_positional) > 0 else None
        var_keyword = [p for p in sig.parameters.values() if p.kind == p.VAR_KEYWORD]
        var_keyword = var_keyword[0] if len(var_keyword) > 0 else None
        default_values = {k: v.default for k, v in sig.parameters.items() if v.default is not inspect.Parameter.empty}
        bound_arguments = sig.bind(*args, **kwargs)
        if apply_defaults:
            bound_arguments.apply_defaults()
        
        storage_inputs = {}
        storage_annotations = {}
        
        for k, v in bound_arguments.arguments.items():
            if var_positional is not None and k == var_positional.name:
                # there are no defaults here
                # we must unwrap any _Ignore instances
                new_v = [arg.value if isinstance(arg, _Ignore) else arg for arg in v]
                ignored_mask = [True if isinstance(arg, _Ignore) else False for arg in v]
                names = [f'{k}_{i}' for i in range(len(new_v))]
                #! update the bound arguments inplace
                bound_arguments.arguments[k] = tuple(new_v)
                for name, val, ignored in zip(names, new_v, ignored_mask):
                    if not ignored:
                        storage_inputs[name] = val
                        storage_annotations[name] = var_positional.annotation
            elif var_keyword is not None and k == var_keyword.name:
                # there are no defaults here either
                new_v = {k: arg.value if isinstance(arg, _Ignore) else arg for k, arg in v.items()}
                ignored_mask = {k: True if isinstance(arg, _Ignore) else False for k, arg in v.items()}
                bound_arguments.arguments[k] = new_v
                for name, val in new_v.items():
                    if not ignored_mask[name]:
                        storage_inputs[name] = val
                        storage_annotations[name] = var_keyword.annotation
            else:
                # could have a default
                if isinstance(v, _Ignore):
                    # regardless of defaults, any _Ignore instance should be ignored
                    bound_arguments.arguments[k] = v.value
                elif k in default_values and isinstance(default_values[k], _NewArgDefault):
                    if isinstance(v, Ref) and self.unwrap(v) == default_values[k].value:
                        # the value is wrapped
                        bound_arguments.arguments[k] = default_values[k].value
                    elif v == default_values[k].value:
                        # the value is unwrapped
                        bound_arguments.arguments[k] = default_values[k].value
                    else:
                        # the given value is different from the default
                        storage_inputs[k] = v
                        storage_annotations[k] = sig.parameters[k].annotation
                else:
                    # this is a regular argument
                    storage_inputs[k] = v
                    storage_annotations[k] = sig.parameters[k].annotation
        return bound_arguments, storage_inputs, storage_annotations
        
    def call_internal(
        self,
        op: Op,
        storage_inputs: Dict[str, Any],
        storage_tps: Dict[str, Type],
        bound_arguments: Optional[inspect.BoundArguments] = None,
    ) -> Tuple[Dict[str, Ref], Call, List[Call]]:
        """
        Main function to call an op, operating on the representations used
        internally by the storage.
        """
        ### wrap the inputs
        if not op.__structural__: logger.debug(f"Calling {op.name} with args {bound_arguments}.")

        wrapped_inputs = {}
        input_calls = []
        for k, v in storage_inputs.items():
            wrapped_inputs[k], struct_calls = self.construct(tp=storage_tps[k], val=v)
            input_calls.extend(struct_calls)
        if len(input_calls) > 0:
            if not op.__structural__: logger.debug(f"Collected {len(input_calls)} calls for inputs.")

        if self.versioned:
            suspended_trace_obj = self.cached_versioner.TracerCls.get_active_trace_obj()
            self.cached_versioner.TracerCls.set_active_trace_obj(trace_obj=None)
        else:
            suspended_trace_obj = None

        ### check for the call
        pre_call_id = op.get_pre_call_id(wrapped_inputs)
        call_option = self.lookup_call(
            op=op,
            pre_call_uid=pre_call_id,
            inputs=wrapped_inputs,
            code_state=self.guess_code_state() if self.versioned else None,
            versioner=self.cached_versioner if self.versioned else None,
        )
        tracer_option = (
            self.cached_versioner.make_tracer() if self.versioned else None
        )

        call_exists = (call_option is not None)
        if call_exists:
            call_hid = call_option.hid
            if not op.__structural__: logger.debug(f"Call to {op.name} with hid {call_hid} already exists.")
            main_call = call_option
            return main_call.outputs, main_call, input_calls

        ### execute the call if it doesn't exist
        if not op.__structural__: 
            # logger.debug(f"Call to {op.name} with hid {call_hid} does not exist; executing.")
            input_hids = {k: v.hid for k, v in wrapped_inputs.items()}
            logger.debug(f"HIDs of inputs: {input_hids}")
        # call the function
        f, sig = op.f, inspect.signature(op.f)
        if op.__structural__:
            returns = f(**wrapped_inputs)
        else:
            #! guard against side effects
            cids_before = {k: v.cid for k, v in wrapped_inputs.items()}
            raw_values = {k: self.unwrap(v) for k, v in wrapped_inputs.items()}
            #! call the function
            args, kwargs = bound_arguments.args, bound_arguments.kwargs
            args = self.unwrap(args)
            kwargs = self.unwrap(kwargs)

            if tracer_option is not None:
                tracer = tracer_option
                with tracer:
                    if isinstance(tracer, DecTracer):
                        node = tracer.register_call(func=op.f)
                    returns = f(*args, **kwargs)
                    if isinstance(tracer, DecTracer):
                        tracer.register_return(node=node)
            else:
                returns = f(*args, **kwargs)

            if not op.__allow_side_effects__:
                # capture changes in the inputs; TODO: this is hacky; ideally, we would
                # avoid calling `construct` and instead recurse on the values, looking for differences.
                cids_after = {
                    k: self.construct(tp=storage_tps[k], val=v)[0].cid
                    for k, v in raw_values.items()
                }
                changed_inputs = {k for k in cids_before if cids_before[k] != cids_after[k]}
                if len(changed_inputs) > 0:
                    raise ValueError(
                        f"Function {f.__name__} has side effects on inputs {changed_inputs}; aborting call."
                    )


        if tracer_option is not None:
            # check the trace against the code state hypothesis
            self.cached_versioner.apply_state_hypothesis(
                hypothesis=self.code_state, trace_result=tracer_option.graph.nodes
            )
            # update the global topology and code state
            self.cached_versioner.update_global_topology(graph=tracer_option.graph)
            self.code_state.add_globals_from(graph=tracer_option.graph)

        content_version, semantic_version = (
            self.cached_versioner.get_version_ids(
                pre_call_uid=pre_call_id,
                tracer_option=tracer_option,
                is_recompute=False,
            )
            if self.versioned
            else (None, None)
        )

        # wrap the outputs
        outputs_dict, outputs_annotations = parse_returns(
            sig=sig, returns=returns, nout=op.nout, output_names=op.output_names
        )
        output_tps = {
            k: Type.from_annotation(annotation=v)
            for k, v in outputs_annotations.items()
        }
        call_content_id = op.get_call_content_id(wrapped_inputs, semantic_version=semantic_version)
        call_hid = op.get_call_history_id(wrapped_inputs, semantic_version=semantic_version)
        output_history_ids = op.get_output_history_ids(
            call_history_id=call_hid, output_names=list(outputs_dict.keys())
        )

        wrapped_outputs = {}
        output_calls = []
        for k, v in outputs_dict.items():
            if isinstance(v, Ref):
                wrapped_outputs[k] = v.with_hid(hid=output_history_ids[k])
            elif isinstance(output_tps[k], AtomType):
                wrapped_outputs[k] = wrap_atom(v, history_id=output_history_ids[k])
            else:  # recurse on types
                start, _ = self.construct(tp=output_tps[k], val=v)
                start = start.with_hid(hid=output_history_ids[k])
                final, output_calls_for_output = self.destruct(start, tp=output_tps[k])
                output_calls.extend(output_calls_for_output)
                wrapped_outputs[k] = final
        main_call = Call(
            op=op,
            cid=call_content_id,
            hid=call_hid,
            inputs=wrapped_inputs,
            outputs=wrapped_outputs,
            semantic_version=semantic_version,
            content_version=content_version,
        )
        return main_call.outputs, main_call, input_calls + output_calls

    ############################################################################
    ### versioning
    ############################################################################
    @transaction
    def get_versioner(self, conn: Optional[sqlite3.Connection] = None) -> Versioner:
        result = self.sources["versioner"]
        if result is None:
            raise ValueError("This storage is not versioned.")
        return result

    @property
    def versioned(self) -> bool:
        return self._versioned

    @transaction
    def guess_code_state(
        self, versioner: Optional[Versioner] = None, conn: Optional[sqlite3.Connection] = None
    ) -> CodeState:
        if versioner is None:
            versioner = self.get_versioner(conn=conn)
        return versioner.guess_code_state()

    @transaction
    def sync_code(
        self, conn: Optional[sqlite3.Connection] = None
    ) -> Tuple[Versioner, CodeState]:
        versioner = self.get_versioner(conn=conn)
        code_state = self.guess_code_state(versioner=versioner, conn=conn)
        versioner.sync_codebase(code_state=code_state)
        return versioner, code_state

    @transaction
    def sync_component(
        self,
        component: types.FunctionType,
        is_semantic_change: Optional[bool],
        conn: Optional[sqlite3.Connection] = None,
    ):
        # low-level versioning
        dep_key = get_dep_key_from_func(func=component)
        versioner = self.get_versioner(conn=conn)
        code_state = self.guess_code_state(versioner=versioner, conn=conn)
        result = versioner.sync_component(
            component=dep_key,
            is_semantic_change=is_semantic_change,
            code_state=code_state,
        )
        self.version_adapter.dump_state(state=versioner, conn=conn)
        return result

    @transaction
    def _show_version_data(
        self,
        f: Union[Callable, "funcs.FuncInterface"],
        deps: bool = True,
        meta: bool = False,
        plain: bool = False,
        compact: bool = False,
        conn: Optional[sqlite3.Connection] = None,
    ):
        # show the versions of a function, with/without its dependencies
        func = extract_func_obj(obj=f, strict=True)
        component = get_dep_key_from_func(func=func)
        versioner = self.get_versioner(conn=conn)
        if deps:
            versioner.show_versions(
                component=component,
                include_metadata=meta,
                plain=plain,
            )
        else:
            versioner.component_dags[component].show(
                compact=compact, plain=plain, include_metadata=meta
            )

    @transaction
    def versions(
        self,
        f: Union[Callable, "funcs.FuncInterface"],
        meta: bool = False,
        plain: bool = False,
        conn: Optional[sqlite3.Connection] = None,
    ):
        self._show_version_data(
            f=f,
            deps=True,
            meta=meta,
            plain=plain,
            compact=False,
            conn=conn,
        )

    # @transaction
    # def sources(
    #     self,
    #     f: Union[Callable, "funcs.FuncInterface"],
    #     meta: bool = False,
    #     plain: bool = False,
    #     compact: bool = False,
    #     conn: Optional[sqlite3.Connection] = None,
    # ):
    #     func = extract_func_obj(obj=f, strict=True)
    #     component = get_dep_key_from_func(func=func)
    #     versioner = self.get_versioner(conn=conn)
    #     print(
    #         f"Revision history for the source code of function {component[1]} from module {component[0]} "
    #         '("===HEAD===" is the current version):'
    #     )
    #     versioner.component_dags[component].show(
    #         compact=compact, plain=plain, include_metadata=meta
    #     )

    @transaction
    def code(
        self, version_id: str, meta: bool = False, conn: Optional[sqlite3.Connection] = None
    ):
        # show a copy-pastable version of the code for a given version id. Plain
        # by design.
        result = self.get_code(version_id=version_id, show=False, meta=meta, conn=conn)
        print(result)

    @transaction
    def get_code(
        self,
        version_id: str,
        show: bool = True,
        meta: bool = False,
        conn: Optional[sqlite3.Connection] = None,
    ) -> str:
        versioner = self.get_versioner(conn=conn)
        for dag in versioner.component_dags.values():
            if version_id in dag.commits.keys():
                text = dag.get_content(commit=version_id)
                if show:
                    print(text)
                return text
        for (
            content_version,
            version,
        ) in versioner.get_flat_versions().items():
            if version_id == content_version:
                raw_string = versioner.present_dependencies(
                    commits=version.semantic_expansion,
                    include_metadata=meta,
                )
                if show:
                    print(raw_string)
                return raw_string
        raise ValueError(f"version id {version_id} not found")

    @transaction
    def diff(
        self,
        id_1: str,
        id_2: str,
        context_lines: int = 2,
        conn: Optional[sqlite3.Connection] = None,
    ):
        code_1: str = self.get_code(version_id=id_1, show=False, conn=conn)
        code_2: str = self.get_code(version_id=id_2, show=False, conn=conn)
        print(
            _get_colorized_diff(current=code_1, new=code_2, context_lines=context_lines)
        )

    ############################################################################
    ### user-facing functions
    ############################################################################
    def cf(
        self, source: Union[Op, Ref, Iterable[Ref], Iterable[str]]
    ) -> "ComputationFrame":
        """
        Main user-facing function to create a computation frame.
        """
        if isinstance(source, Op):
            return ComputationFrame.from_op(storage=self, f=source)
        elif isinstance(source, Ref):
            return ComputationFrame.from_refs(refs=[source], storage=self)
        elif all(isinstance(elt, Ref) for elt in source):
            return ComputationFrame.from_refs(refs=source, storage=self)
        elif all(isinstance(elt, str) for elt in source):
            # must be hids
            refs = [self.load_ref(hid, lazy=True) for hid in source]
            return ComputationFrame.from_refs(refs=refs, storage=self)
        else:
            raise ValueError("Invalid input to `cf`")

    def call(
        self, __op__: Op, args, kwargs, __config__: Optional[dict] = None
    ) -> Union[Tuple[Ref, ...], Ref]:
        __config__ = {} if __config__ is None else __config__
        bound_arguments, storage_inputs, storage_annotations = self.parse_args(
            sig=inspect.signature(__op__.f),
            args=args,
            kwargs=kwargs,
            apply_defaults=True,
        )

        storage_tps = {
            k: Type.from_annotation(annotation=v) for k, v in storage_annotations.items()
        }
        res, main_call, calls = self.call_internal(
            op=__op__,
            storage_inputs = storage_inputs,
            bound_arguments = bound_arguments,
            storage_tps=storage_tps,
        )
        if __config__.get("save_calls", False):
            self.save_call(main_call)
            for call in calls:
                self.save_call(call)
        ord_outputs = __op__.get_ordered_outputs(main_call.outputs)
        if len(ord_outputs) == 1:
            return ord_outputs[0]
        else:
            return ord_outputs

    def __enter__(self) -> "Storage":
        Context.current_context = Context(storage=self)
        if self.versioned:
            versioner, code_state = self.sync_code()
            self.cached_versioner = versioner
            self.code_state = code_state
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        Context.current_context = None
        try:
            self.commit()
        except Exception as e:
            raise e
        finally:
            self.cached_versioner = None
            self.code_state = None

    

from .cf import ComputationFrame


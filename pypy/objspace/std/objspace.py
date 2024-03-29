import __builtin__
from pypy.interpreter import special
from pypy.interpreter.baseobjspace import ObjSpace, W_Root
from pypy.interpreter.error import OperationError, oefmt
from pypy.interpreter.typedef import get_unique_interplevel_subclass
from pypy.objspace.std import frame, transparent, callmethod
from pypy.objspace.descroperation import DescrOperation, raiseattrerror
from rpython.rlib.objectmodel import instantiate, specialize, is_annotation_constant
from rpython.rlib.debug import make_sure_not_resized
from rpython.rlib.rarithmetic import base_int, widen, is_valid_int
from rpython.rlib.objectmodel import import_from_mixin
from rpython.rlib import jit

# Object imports
from pypy.objspace.std.basestringtype import basestring_typedef
from pypy.objspace.std.boolobject import W_BoolObject
from pypy.objspace.std.bufferobject import W_Buffer
from pypy.objspace.std.bytearrayobject import W_BytearrayObject
from pypy.objspace.std.bytesobject import W_AbstractBytesObject, W_BytesObject, wrapstr
from pypy.objspace.std.complexobject import W_ComplexObject
from pypy.objspace.std.dictmultiobject import W_DictMultiObject, W_DictObject
from pypy.objspace.std.floatobject import W_FloatObject
from pypy.objspace.std.intobject import W_IntObject, setup_prebuilt, wrapint
from pypy.objspace.std.iterobject import W_AbstractSeqIterObject, W_SeqIterObject
from pypy.objspace.std.listobject import W_ListObject
from pypy.objspace.std.longobject import W_LongObject, newlong
from pypy.objspace.std.memoryobject import W_MemoryView
from pypy.objspace.std.noneobject import W_NoneObject
from pypy.objspace.std.objectobject import W_ObjectObject
from pypy.objspace.std.setobject import W_SetObject, W_FrozensetObject
from pypy.objspace.std.sliceobject import W_SliceObject
from pypy.objspace.std.tupleobject import W_AbstractTupleObject, W_TupleObject
from pypy.objspace.std.typeobject import W_TypeObject, TypeCache
from pypy.objspace.std.unicodeobject import W_UnicodeObject, wrapunicode


class StdObjSpace(ObjSpace):
    """The standard object space, implementing a general-purpose object
    library in Restricted Python."""
    import_from_mixin(DescrOperation)

    def initialize(self):
        """NOT_RPYTHON: only for initializing the space

        Setup all the object types and implementations.
        """

        setup_prebuilt(self)
        self.FrameClass = frame.build_frame(self)
        self.StringObjectCls = W_BytesObject
        self.UnicodeObjectCls = W_UnicodeObject

        # singletons
        self.w_None = W_NoneObject.w_None
        self.w_False = W_BoolObject.w_False
        self.w_True = W_BoolObject.w_True
        self.w_NotImplemented = self.wrap(special.NotImplemented())
        self.w_Ellipsis = self.wrap(special.Ellipsis())

        # types
        builtin_type_classes = {
            W_BoolObject.typedef: W_BoolObject,
            W_Buffer.typedef: W_Buffer,
            W_BytearrayObject.typedef: W_BytearrayObject,
            W_BytesObject.typedef: W_BytesObject,
            W_ComplexObject.typedef: W_ComplexObject,
            W_DictMultiObject.typedef: W_DictMultiObject,
            W_FloatObject.typedef: W_FloatObject,
            W_IntObject.typedef: W_IntObject,
            W_AbstractSeqIterObject.typedef: W_AbstractSeqIterObject,
            W_ListObject.typedef: W_ListObject,
            W_LongObject.typedef: W_LongObject,
            W_MemoryView.typedef: W_MemoryView,
            W_NoneObject.typedef: W_NoneObject,
            W_ObjectObject.typedef: W_ObjectObject,
            W_SetObject.typedef: W_SetObject,
            W_FrozensetObject.typedef: W_FrozensetObject,
            W_SliceObject.typedef: W_SliceObject,
            W_TupleObject.typedef: W_TupleObject,
            W_TypeObject.typedef: W_TypeObject,
            W_UnicodeObject.typedef: W_UnicodeObject,
        }
        if self.config.objspace.std.withstrbuf:
            builtin_type_classes[W_BytesObject.typedef] = W_AbstractBytesObject

        self.builtin_types = {}
        self._interplevel_classes = {}
        for typedef, cls in builtin_type_classes.items():
            w_type = self.gettypeobject(typedef)
            self.builtin_types[typedef.name] = w_type
            setattr(self, 'w_' + typedef.name, w_type)
            self._interplevel_classes[w_type] = cls
        self.builtin_types["NotImplemented"] = self.w_NotImplemented
        self.builtin_types["Ellipsis"] = self.w_Ellipsis
        self.w_basestring = self.builtin_types['basestring'] = \
            self.gettypeobject(basestring_typedef)

        # exceptions & builtins
        self.make_builtins()

        # the type of old-style classes
        self.w_classobj = self.builtin.get('__metaclass__')

        # final setup
        self.setup_builtin_modules()
        # Adding transparent proxy call
        if self.config.objspace.std.withtproxy:
            transparent.setup(self)

    def get_builtin_types(self):
        return self.builtin_types

    def createexecutioncontext(self):
        # add space specific fields to execution context
        # note that this method must not call space methods that might need an
        # execution context themselves (e.g. nearly all space methods)
        ec = ObjSpace.createexecutioncontext(self)
        ec._py_repr = None
        return ec

    def gettypefor(self, cls):
        return self.gettypeobject(cls.typedef)

    def gettypeobject(self, typedef):
        # typeobject.TypeCache maps a TypeDef instance to its
        # unique-for-this-space W_TypeObject instance
        assert typedef is not None
        return self.fromcache(TypeCache).getorbuild(typedef)

    def wrapbytes(self, x):
        return wrapstr(self, x)

    @specialize.argtype(1)
    def wrap(self, x):
        "Wraps the Python value 'x' into one of the wrapper classes."
        # You might notice that this function is rather conspicuously
        # not RPython.  We can get away with this because the function
        # is specialized (see after the function body).  Also worth
        # noting is that the isinstance's involving integer types
        # behave rather differently to how you might expect during
        # annotation (see pypy/annotation/builtin.py)
        if x is None:
            return self.w_None
        if isinstance(x, OperationError):
            raise TypeError, ("attempt to wrap already wrapped exception: %s"%
                              (x,))
        if isinstance(x, int):
            if isinstance(x, bool):
                return self.newbool(x)
            else:
                return self.newint(x)
        if isinstance(x, str):
            return wrapstr(self, x)
        if isinstance(x, unicode):
            return wrapunicode(self, x)
        if isinstance(x, float):
            return W_FloatObject(x)
        if isinstance(x, W_Root):
            w_result = x.__spacebind__(self)
            #print 'wrapping', x, '->', w_result
            return w_result
        if isinstance(x, base_int):
            if self.config.objspace.std.withsmalllong:
                from pypy.objspace.std.smalllongobject import W_SmallLongObject
                from rpython.rlib.rarithmetic import r_longlong, r_ulonglong
                from rpython.rlib.rarithmetic import longlongmax
                if (not isinstance(x, r_ulonglong)
                    or x <= r_ulonglong(longlongmax)):
                    return W_SmallLongObject(r_longlong(x))
            x = widen(x)
            if isinstance(x, int):
                return self.newint(x)
            else:
                return W_LongObject.fromrarith_int(x)
        return self._wrap_not_rpython(x)

    def _wrap_not_rpython(self, x):
        "NOT_RPYTHON"
        # _____ this code is here to support testing only _____

        # we might get there in non-translated versions if 'x' is
        # a long that fits the correct range.
        if is_valid_int(x):
            return self.newint(x)

        # wrap() of a container works on CPython, but the code is
        # not RPython.  Don't use -- it is kept around mostly for tests.
        # Use instead newdict(), newlist(), newtuple().
        if isinstance(x, dict):
            items_w = [(self.wrap(k), self.wrap(v)) for (k, v) in x.iteritems()]
            r = self.newdict()
            r.initialize_content(items_w)
            return r
        if isinstance(x, tuple):
            wrappeditems = [self.wrap(item) for item in list(x)]
            return self.newtuple(wrappeditems)
        if isinstance(x, list):
            wrappeditems = [self.wrap(item) for item in x]
            return self.newlist(wrappeditems)

        # The following cases are even stranger.
        # Really really only for tests.
        if type(x) is long:
            return self.wraplong(x)
        if isinstance(x, slice):
            return W_SliceObject(self.wrap(x.start),
                                 self.wrap(x.stop),
                                 self.wrap(x.step))
        if isinstance(x, complex):
            return W_ComplexObject(x.real, x.imag)

        if isinstance(x, set):
            res = W_SetObject(self, self.newlist([self.wrap(item) for item in x]))
            return res

        if isinstance(x, frozenset):
            wrappeditems = [self.wrap(item) for item in x]
            return W_FrozensetObject(self, wrappeditems)

        if x is __builtin__.Ellipsis:
            # '__builtin__.Ellipsis' avoids confusion with special.Ellipsis
            return self.w_Ellipsis

        raise OperationError(self.w_RuntimeError,
            self.wrap("refusing to wrap cpython value %r" % (x,))
        )

    def wrap_exception_cls(self, x):
        """NOT_RPYTHON"""
        if hasattr(self, 'w_' + x.__name__):
            w_result = getattr(self, 'w_' + x.__name__)
            return w_result
        return None

    def wraplong(self, x):
        "NOT_RPYTHON"
        if self.config.objspace.std.withsmalllong:
            from rpython.rlib.rarithmetic import r_longlong
            try:
                rx = r_longlong(x)
            except OverflowError:
                pass
            else:
                from pypy.objspace.std.smalllongobject import \
                                               W_SmallLongObject
                return W_SmallLongObject(rx)
        return W_LongObject.fromlong(x)

    def unwrap(self, w_obj):
        """NOT_RPYTHON"""
        # _____ this code is here to support testing only _____
        if isinstance(w_obj, W_Root):
            return w_obj.unwrap(self)
        raise TypeError("cannot unwrap: %r" % w_obj)

    def newint(self, intval):
        return wrapint(self, intval)

    def newfloat(self, floatval):
        return W_FloatObject(floatval)

    def newcomplex(self, realval, imagval):
        return W_ComplexObject(realval, imagval)

    def unpackcomplex(self, w_complex):
        from pypy.objspace.std.complexobject import unpackcomplex
        return unpackcomplex(self, w_complex)

    def newlong(self, val): # val is an int
        if self.config.objspace.std.withsmalllong:
            from pypy.objspace.std.smalllongobject import W_SmallLongObject
            return W_SmallLongObject.fromint(val)
        return W_LongObject.fromint(self, val)

    def newlong_from_rbigint(self, val):
        return newlong(self, val)

    def newtuple(self, list_w):
        from pypy.objspace.std.tupleobject import wraptuple
        assert isinstance(list_w, list)
        make_sure_not_resized(list_w)
        return wraptuple(self, list_w)

    def newlist(self, list_w, sizehint=-1):
        assert not list_w or sizehint == -1
        return W_ListObject(self, list_w, sizehint)

    def newlist_bytes(self, list_s):
        return W_ListObject.newlist_bytes(self, list_s)

    def newlist_unicode(self, list_u):
        return W_ListObject.newlist_unicode(self, list_u)

    def newlist_int(self, list_i):
        return W_ListObject.newlist_int(self, list_i)

    def newlist_float(self, list_f):
        return W_ListObject.newlist_float(self, list_f)

    def newdict(self, module=False, instance=False, kwargs=False,
                strdict=False):
        return W_DictMultiObject.allocate_and_init_instance(
                self, module=module, instance=instance,
                strdict=strdict, kwargs=kwargs)

    def newset(self, iterable_w=None):
        if iterable_w is None:
            return W_SetObject(self, None)
        return W_SetObject(self, self.newtuple(iterable_w))

    def newfrozenset(self, iterable_w=None):
        if iterable_w is None:
            return W_FrozensetObject(self, None)
        return W_FrozensetObject(self, self.newtuple(iterable_w))

    def newslice(self, w_start, w_end, w_step):
        return W_SliceObject(w_start, w_end, w_step)

    def newseqiter(self, w_obj):
        return W_SeqIterObject(w_obj)

    def newbuffer(self, w_obj):
        return W_Buffer(w_obj)

    def type(self, w_obj):
        jit.promote(w_obj.__class__)
        return w_obj.getclass(self)

    def lookup(self, w_obj, name):
        w_type = self.type(w_obj)
        return w_type.lookup(name)
    lookup._annspecialcase_ = 'specialize:lookup'

    def lookup_in_type_where(self, w_type, name):
        return w_type.lookup_where(name)
    lookup_in_type_where._annspecialcase_ = 'specialize:lookup_in_type_where'

    def lookup_in_type_starting_at(self, w_type, w_starttype, name):
        """ Only supposed to be used to implement super, w_starttype
        and w_type are the same as for super(starttype, type)
        """
        assert isinstance(w_type, W_TypeObject)
        assert isinstance(w_starttype, W_TypeObject)
        return w_type.lookup_starting_at(w_starttype, name)

    def allocate_instance(self, cls, w_subtype):
        """Allocate the memory needed for an instance of an internal or
        user-defined type, without actually __init__ializing the instance."""
        w_type = self.gettypeobject(cls.typedef)
        if self.is_w(w_type, w_subtype):
            instance = instantiate(cls)
        elif cls.typedef.acceptable_as_base_class:
            # the purpose of the above check is to avoid the code below
            # to be annotated at all for 'cls' if it is not necessary
            w_subtype = w_type.check_user_subclass(w_subtype)
            if cls.typedef.applevel_subclasses_base is not None:
                cls = cls.typedef.applevel_subclasses_base
            #
            if (self.config.objspace.std.withmapdict and cls is W_ObjectObject
                    and not w_subtype.needsdel):
                from pypy.objspace.std.mapdict import get_subclass_of_correct_size
                subcls = get_subclass_of_correct_size(self, cls, w_subtype)
            else:
                subcls = get_unique_interplevel_subclass(
                        self.config, cls, w_subtype.hasdict,
                        w_subtype.layout.nslots != 0,
                        w_subtype.needsdel, w_subtype.weakrefable)
            instance = instantiate(subcls)
            assert isinstance(instance, cls)
            instance.user_setup(self, w_subtype)
        else:
            raise oefmt(self.w_TypeError,
                        "%N.__new__(%N): only for the type %N",
                        w_type, w_subtype, w_type)
        return instance
    allocate_instance._annspecialcase_ = "specialize:arg(1)"

    # two following functions are almost identical, but in fact they
    # have different return type. First one is a resizable list, second
    # one is not

    def _wrap_expected_length(self, expected, got):
        return OperationError(self.w_ValueError,
                self.wrap("expected length %d, got %d" % (expected, got)))

    def unpackiterable(self, w_obj, expected_length=-1):
        if isinstance(w_obj, W_AbstractTupleObject) and self._uses_tuple_iter(w_obj):
            t = w_obj.getitems_copy()
        elif type(w_obj) is W_ListObject:
            t = w_obj.getitems_copy()
        else:
            return ObjSpace.unpackiterable(self, w_obj, expected_length)
        if expected_length != -1 and len(t) != expected_length:
            raise self._wrap_expected_length(expected_length, len(t))
        return t

    @specialize.arg(3)
    def fixedview(self, w_obj, expected_length=-1, unroll=False):
        """ Fast paths
        """
        if isinstance(w_obj, W_AbstractTupleObject) and self._uses_tuple_iter(w_obj):
            t = w_obj.tolist()
        elif type(w_obj) is W_ListObject:
            if unroll:
                t = w_obj.getitems_unroll()
            else:
                t = w_obj.getitems_fixedsize()
        else:
            if unroll:
                return make_sure_not_resized(ObjSpace.unpackiterable_unroll(
                    self, w_obj, expected_length))
            else:
                return make_sure_not_resized(ObjSpace.unpackiterable(
                    self, w_obj, expected_length)[:])
        if expected_length != -1 and len(t) != expected_length:
            raise self._wrap_expected_length(expected_length, len(t))
        return make_sure_not_resized(t)

    def fixedview_unroll(self, w_obj, expected_length):
        assert expected_length >= 0
        return self.fixedview(w_obj, expected_length, unroll=True)

    def listview_no_unpack(self, w_obj):
        if type(w_obj) is W_ListObject:
            return w_obj.getitems()
        elif isinstance(w_obj, W_AbstractTupleObject) and self._uses_tuple_iter(w_obj):
            return w_obj.getitems_copy()
        elif isinstance(w_obj, W_ListObject) and self._uses_list_iter(w_obj):
            return w_obj.getitems()
        else:
            return None

    def listview(self, w_obj, expected_length=-1):
        t = self.listview_no_unpack(w_obj)
        if t is None:
            return ObjSpace.unpackiterable(self, w_obj, expected_length)
        if expected_length != -1 and len(t) != expected_length:
            raise self._wrap_expected_length(expected_length, len(t))
        return t

    def listview_bytes(self, w_obj):
        # note: uses exact type checking for objects with strategies,
        # and isinstance() for others.  See test_listobject.test_uses_custom...
        if type(w_obj) is W_ListObject:
            return w_obj.getitems_bytes()
        if type(w_obj) is W_DictObject:
            return w_obj.listview_bytes()
        if type(w_obj) is W_SetObject or type(w_obj) is W_FrozensetObject:
            return w_obj.listview_bytes()
        if isinstance(w_obj, W_BytesObject) and self._uses_no_iter(w_obj):
            return w_obj.listview_bytes()
        if isinstance(w_obj, W_ListObject) and self._uses_list_iter(w_obj):
            return w_obj.getitems_bytes()
        return None

    def listview_unicode(self, w_obj):
        # note: uses exact type checking for objects with strategies,
        # and isinstance() for others.  See test_listobject.test_uses_custom...
        if type(w_obj) is W_ListObject:
            return w_obj.getitems_unicode()
        if type(w_obj) is W_DictObject:
            return w_obj.listview_unicode()
        if type(w_obj) is W_SetObject or type(w_obj) is W_FrozensetObject:
            return w_obj.listview_unicode()
        if isinstance(w_obj, W_UnicodeObject) and self._uses_no_iter(w_obj):
            return w_obj.listview_unicode()
        if isinstance(w_obj, W_ListObject) and self._uses_list_iter(w_obj):
            return w_obj.getitems_unicode()
        return None

    def listview_int(self, w_obj):
        if type(w_obj) is W_ListObject:
            return w_obj.getitems_int()
        if type(w_obj) is W_DictObject:
            return w_obj.listview_int()
        if type(w_obj) is W_SetObject or type(w_obj) is W_FrozensetObject:
            return w_obj.listview_int()
        if isinstance(w_obj, W_ListObject) and self._uses_list_iter(w_obj):
            return w_obj.getitems_int()
        return None

    def listview_float(self, w_obj):
        if type(w_obj) is W_ListObject:
            return w_obj.getitems_float()
        # dict and set don't have FloatStrategy, so we can just ignore them
        # for now
        if isinstance(w_obj, W_ListObject) and self._uses_list_iter(w_obj):
            return w_obj.getitems_float()
        return None

    def view_as_kwargs(self, w_dict):
        if type(w_dict) is W_DictObject:
            return w_dict.view_as_kwargs()
        return (None, None)

    def _uses_list_iter(self, w_obj):
        from pypy.objspace.descroperation import list_iter
        return self.lookup(w_obj, '__iter__') is list_iter(self)

    def _uses_tuple_iter(self, w_obj):
        from pypy.objspace.descroperation import tuple_iter
        return self.lookup(w_obj, '__iter__') is tuple_iter(self)

    def _uses_no_iter(self, w_obj):
        return self.lookup(w_obj, '__iter__') is None

    def sliceindices(self, w_slice, w_length):
        if isinstance(w_slice, W_SliceObject):
            a, b, c = w_slice.indices3(self, self.int_w(w_length))
            return (a, b, c)
        w_indices = self.getattr(w_slice, self.wrap('indices'))
        w_tup = self.call_function(w_indices, w_length)
        l_w = self.unpackiterable(w_tup)
        if not len(l_w) == 3:
            raise OperationError(self.w_ValueError,
                                 self.wrap("Expected tuple of length 3"))
        return self.int_w(l_w[0]), self.int_w(l_w[1]), self.int_w(l_w[2])

    _DescrOperation_is_true = is_true
    _DescrOperation_getattr = getattr

    def is_true(self, w_obj):
        # a shortcut for performance
        if type(w_obj) is W_BoolObject:
            return bool(w_obj.intval)
        return self._DescrOperation_is_true(w_obj)

    def getattr(self, w_obj, w_name):
        if not self.config.objspace.std.getattributeshortcut:
            return self._DescrOperation_getattr(w_obj, w_name)
        # an optional shortcut for performance

        w_type = self.type(w_obj)
        w_descr = w_type.getattribute_if_not_from_object()
        if w_descr is not None:
            return self._handle_getattribute(w_descr, w_obj, w_name)

        # fast path: XXX this is duplicating most of the logic
        # from the default __getattribute__ and the getattr() method...
        name = self.str_w(w_name)
        w_descr = w_type.lookup(name)
        e = None
        if w_descr is not None:
            w_get = None
            is_data = self.is_data_descr(w_descr)
            if is_data:
                w_get = self.lookup(w_descr, "__get__")
            if w_get is None:
                w_value = w_obj.getdictvalue(self, name)
                if w_value is not None:
                    return w_value
                if not is_data:
                    w_get = self.lookup(w_descr, "__get__")
            if w_get is not None:
                # __get__ is allowed to raise an AttributeError to trigger
                # use of __getattr__.
                try:
                    return self.get_and_call_function(w_get, w_descr, w_obj,
                                                      w_type)
                except OperationError, e:
                    if not e.match(self, self.w_AttributeError):
                        raise
            else:
                return w_descr
        else:
            w_value = w_obj.getdictvalue(self, name)
            if w_value is not None:
                return w_value

        w_descr = self.lookup(w_obj, '__getattr__')
        if w_descr is not None:
            return self.get_and_call_function(w_descr, w_obj, w_name)
        elif e is not None:
            raise e
        else:
            raiseattrerror(self, w_obj, name)

    def finditem_str(self, w_obj, key):
        """ Perform a getitem on w_obj with key (string). Returns found
        element or None on element not found.

        performance shortcut to avoid creating the OperationError(KeyError)
        and allocating W_BytesObject
        """
        if (isinstance(w_obj, W_DictMultiObject) and
                not w_obj.user_overridden_class):
            return w_obj.getitem_str(key)
        return ObjSpace.finditem_str(self, w_obj, key)

    def finditem(self, w_obj, w_key):
        """ Perform a getitem on w_obj with w_key (any object). Returns found
        element or None on element not found.

        performance shortcut to avoid creating the OperationError(KeyError).
        """
        if (isinstance(w_obj, W_DictMultiObject) and
                not w_obj.user_overridden_class):
            return w_obj.getitem(w_key)
        return ObjSpace.finditem(self, w_obj, w_key)

    def setitem_str(self, w_obj, key, w_value):
        """ Same as setitem, but takes string instead of any wrapped object
        """
        if (isinstance(w_obj, W_DictMultiObject) and
                not w_obj.user_overridden_class):
            w_obj.setitem_str(key, w_value)
        else:
            self.setitem(w_obj, self.wrap(key), w_value)

    def getindex_w(self, w_obj, w_exception, objdescr=None):
        if type(w_obj) is W_IntObject:
            return w_obj.intval
        return ObjSpace.getindex_w(self, w_obj, w_exception, objdescr)

    def unicode_from_object(self, w_obj):
        from pypy.objspace.std.unicodeobject import unicode_from_object
        return unicode_from_object(self, w_obj)

    def call_method(self, w_obj, methname, *arg_w):
        return callmethod.call_method_opt(self, w_obj, methname, *arg_w)

    def _type_issubtype(self, w_sub, w_type):
        if isinstance(w_sub, W_TypeObject) and isinstance(w_type, W_TypeObject):
            return self.wrap(w_sub.issubtype(w_type))
        raise OperationError(self.w_TypeError, self.wrap("need type objects"))

    @specialize.arg_or_var(2)
    def _type_isinstance(self, w_inst, w_type):
        if not isinstance(w_type, W_TypeObject):
            raise OperationError(self.w_TypeError,
                                 self.wrap("need type object"))
        if is_annotation_constant(w_type):
            cls = self._get_interplevel_cls(w_type)
            if cls is not None:
                assert w_inst is not None
                if isinstance(w_inst, cls):
                    return True
        return self.type(w_inst).issubtype(w_type)

    @specialize.memo()
    def _get_interplevel_cls(self, w_type):
        if not hasattr(self, "_interplevel_classes"):
            return None # before running initialize
        return self._interplevel_classes.get(w_type, None)

    @specialize.arg(2, 3)
    def is_overloaded(self, w_obj, tp, method):
        return (self.lookup(w_obj, method) is not
                self.lookup_in_type_where(tp, method)[1])

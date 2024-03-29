from pypy.interpreter import gateway
from pypy.interpreter.baseobjspace import W_Root, SpaceCache
from pypy.interpreter.error import oefmt, OperationError
from pypy.interpreter.function import Function, StaticMethod
from pypy.interpreter.typedef import weakref_descr, GetSetProperty,\
     descr_get_dict, dict_descr, Member, TypeDef
from pypy.interpreter.astcompiler.misc import mangle

from rpython.rlib.jit import (promote, elidable_promote, we_are_jitted,
     elidable, dont_look_inside, unroll_safe)
from rpython.rlib.objectmodel import current_object_addr_as_int, compute_hash
from rpython.rlib.rarithmetic import intmask, r_uint

class MutableCell(W_Root):
    def unwrap_cell(self, space):
        raise NotImplementedError("abstract base")

class ObjectMutableCell(MutableCell):
    def __init__(self, w_value=None):
        self.w_value = w_value

    def unwrap_cell(self, space):
        return self.w_value

    def __repr__(self):
        return "<ObjectMutableCell: %s>" % (self.w_value, )


class IntMutableCell(MutableCell):
    def __init__(self, intvalue):
        self.intvalue = intvalue

    def unwrap_cell(self, space):
        return space.wrap(self.intvalue)

    def __repr__(self):
        return "<IntMutableCell: %s>" % (self.intvalue, )


def unwrap_cell(space, w_value):
    if space.config.objspace.std.withtypeversion:
        if isinstance(w_value, MutableCell):
            return w_value.unwrap_cell(space)
    return w_value

def write_cell(space, w_cell, w_value):
    from pypy.objspace.std.intobject import W_IntObject
    if w_cell is None:
        # attribute does not exist at all, write it without a cell first
        return w_value
    if isinstance(w_cell, ObjectMutableCell):
        w_cell.w_value = w_value
        return None
    elif isinstance(w_cell, IntMutableCell) and type(w_value) is W_IntObject:
        w_cell.intvalue = w_value.intval
        return None
    elif space.is_w(w_cell, w_value):
        # If the new value and the current value are the same, don't
        # create a level of indirection, or mutate the version.
        return None
    if type(w_value) is W_IntObject:
        return IntMutableCell(w_value.intval)
    else:
        return ObjectMutableCell(w_value)

class VersionTag(object):
    pass

class MethodCache(object):

    def __init__(self, space):
        assert space.config.objspace.std.withmethodcache
        SIZE = 1 << space.config.objspace.std.methodcachesizeexp
        self.versions = [None] * SIZE
        self.names = [None] * SIZE
        self.lookup_where = [(None, None)] * SIZE
        if space.config.objspace.std.withmethodcachecounter:
            self.hits = {}
            self.misses = {}

    def clear(self):
        None_None = (None, None)
        for i in range(len(self.versions)):
            self.versions[i] = None
        for i in range(len(self.names)):
            self.names[i] = None
        for i in range(len(self.lookup_where)):
            self.lookup_where[i] = None_None


class Layout(object):
    """A Layout is attached to every W_TypeObject to represent the
    layout of instances.  Some W_TypeObjects share the same layout.
    If a W_TypeObject is a base of another, then the layout of
    the first is either the same or a parent layout of the second.
    The Layouts have single inheritance, unlike W_TypeObjects.
    """
    _immutable_ = True

    def __init__(self, typedef, nslots, base_layout=None):
        self.typedef = typedef
        self.nslots = nslots
        self.base_layout = base_layout

    def issublayout(self, parent):
        while self is not parent:
            self = self.base_layout
            if self is None:
                return False
        return True


# possible values of compares_by_identity_status
UNKNOWN = 0
COMPARES_BY_IDENTITY = 1
OVERRIDES_EQ_CMP_OR_HASH = 2

class W_TypeObject(W_Root):
    lazyloaders = {} # can be overridden by specific instances

    # the version_tag changes if the dict or the inheritance hierarchy changes
    # other changes to the type (e.g. the name) leave it unchanged
    _version_tag = None

    _immutable_fields_ = ["flag_heaptype",
                          "flag_cpytype",
                          "flag_abstract?",
                          "flag_sequence_bug_compat",
                          'needsdel',
                          'weakrefable',
                          'hasdict',
                          'layout',
                          'terminator',
                          '_version_tag?',
                          'name?',
                          'mro_w?[*]',
                          ]

    # for config.objspace.std.getattributeshortcut
    # (False is a conservative default, fixed during real usage)
    uses_object_getattribute = False

    # for config.objspace.std.withidentitydict
    compares_by_identity_status = UNKNOWN

    # used to cache the type's __new__ function
    w_new_function = None

    @dont_look_inside
    def __init__(w_self, space, name, bases_w, dict_w,
                 overridetypedef=None, force_new_layout=False):
        w_self.space = space
        w_self.name = name
        w_self.bases_w = bases_w
        w_self.dict_w = dict_w
        w_self.hasdict = False
        w_self.needsdel = False
        w_self.weakrefable = False
        w_self.w_doc = space.w_None
        w_self.weak_subclasses = []
        w_self.flag_heaptype = False
        w_self.flag_cpytype = False
        w_self.flag_abstract = False
        w_self.flag_sequence_bug_compat = False

        if overridetypedef is not None:
            assert not force_new_layout
            layout = setup_builtin_type(w_self, overridetypedef)
        else:
            layout = setup_user_defined_type(w_self, force_new_layout)
        w_self.layout = layout

        if space.config.objspace.std.withtypeversion:
            if not is_mro_purely_of_types(w_self.mro_w):
                pass
            else:
                # the _version_tag should change, whenever the content of
                # dict_w of any of the types in the mro changes, or if the mro
                # itself changes
                w_self._version_tag = VersionTag()
        if space.config.objspace.std.withmapdict:
            from pypy.objspace.std.mapdict import DictTerminator, NoDictTerminator
            if w_self.hasdict:
                w_self.terminator = DictTerminator(space, w_self)
            else:
                w_self.terminator = NoDictTerminator(space, w_self)

    def __repr__(self):
        "NOT_RPYTHON"
        return '<W_TypeObject %r at 0x%x>' % (self.name, id(self))

    def mutated(w_self, key):
        """
        The type is being mutated. key is either the string containing the
        specific attribute which is being deleted/set or None to indicate a
        generic mutation.
        """
        space = w_self.space
        assert w_self.is_heaptype() or w_self.is_cpytype()
        if (not space.config.objspace.std.withtypeversion and
            not space.config.objspace.std.getattributeshortcut and
            not space.config.objspace.std.withidentitydict and
            not space.config.objspace.std.newshortcut):
            return

        if space.config.objspace.std.getattributeshortcut:
            w_self.uses_object_getattribute = False
            # ^^^ conservative default, fixed during real usage

        if space.config.objspace.std.withidentitydict:
            if (key is None or key == '__eq__' or
                key == '__cmp__' or key == '__hash__'):
                w_self.compares_by_identity_status = UNKNOWN

        if space.config.objspace.std.newshortcut:
            w_self.w_new_function = None

        if (space.config.objspace.std.withtypeversion
            and w_self._version_tag is not None):
            w_self._version_tag = VersionTag()

        subclasses_w = w_self.get_subclasses()
        for w_subclass in subclasses_w:
            assert isinstance(w_subclass, W_TypeObject)
            w_subclass.mutated(key)

    def version_tag(w_self):
        if not we_are_jitted() or w_self.is_heaptype():
            return w_self._version_tag
        # prebuilt objects cannot get their version_tag changed
        return w_self._pure_version_tag()

    @elidable_promote()
    def _pure_version_tag(w_self):
        return w_self._version_tag

    def getattribute_if_not_from_object(w_self):
        """ this method returns the applevel __getattribute__ if that is not
        the one from object, in which case it returns None """
        from pypy.objspace.descroperation import object_getattribute
        if not we_are_jitted():
            shortcut = w_self.space.config.objspace.std.getattributeshortcut
            if not shortcut or not w_self.uses_object_getattribute:
                # slow path: look for a custom __getattribute__ on the class
                w_descr = w_self.lookup('__getattribute__')
                # if it was not actually overriden in the class, we remember this
                # fact for the next time.
                if w_descr is object_getattribute(w_self.space):
                    if shortcut:
                        w_self.uses_object_getattribute = True
                else:
                    return w_descr
            return None
        # in the JIT case, just use a lookup, because it is folded away
        # correctly using the version_tag
        w_descr = w_self.lookup('__getattribute__')
        if w_descr is not object_getattribute(w_self.space):
            return w_descr

    def has_object_getattribute(w_self):
        return w_self.getattribute_if_not_from_object() is None

    def compares_by_identity(w_self):
        from pypy.objspace.descroperation import object_hash, type_eq
        if not w_self.space.config.objspace.std.withidentitydict:
            return False # conservative
        #
        if w_self.compares_by_identity_status != UNKNOWN:
            # fast path
            return w_self.compares_by_identity_status == COMPARES_BY_IDENTITY
        #
        default_hash = object_hash(w_self.space)
        my_eq = w_self.lookup('__eq__')
        overrides_eq = (my_eq and my_eq is not type_eq(w_self.space))
        overrides_eq_cmp_or_hash = (overrides_eq or
                                    w_self.lookup('__cmp__') or
                                    w_self.lookup('__hash__') is not default_hash)
        if overrides_eq_cmp_or_hash:
            w_self.compares_by_identity_status = OVERRIDES_EQ_CMP_OR_HASH
        else:
            w_self.compares_by_identity_status = COMPARES_BY_IDENTITY
        return w_self.compares_by_identity_status == COMPARES_BY_IDENTITY

    def ready(w_self):
        for w_base in w_self.bases_w:
            if not isinstance(w_base, W_TypeObject):
                continue
            w_base.add_subclass(w_self)

    # compute a tuple that fully describes the instance layout
    def get_full_instance_layout(w_self):
        layout = w_self.layout
        return (layout, w_self.hasdict, w_self.needsdel, w_self.weakrefable)

    def compute_default_mro(w_self):
        return compute_C3_mro(w_self.space, w_self)

    def getdictvalue(w_self, space, attr):
        if space.config.objspace.std.withtypeversion:
            version_tag = w_self.version_tag()
            if version_tag is not None:
                return unwrap_cell(
                    space,
                    w_self._pure_getdictvalue_no_unwrapping(
                        space, version_tag, attr))
        w_value = w_self._getdictvalue_no_unwrapping(space, attr)
        return unwrap_cell(space, w_value)

    def _getdictvalue_no_unwrapping(w_self, space, attr):
        w_value = w_self.dict_w.get(attr, None)
        if w_self.lazyloaders and w_value is None:
            if attr in w_self.lazyloaders:
                # very clever next line: it forces the attr string
                # to be interned.
                space.new_interned_str(attr)
                loader = w_self.lazyloaders[attr]
                del w_self.lazyloaders[attr]
                w_value = loader()
                if w_value is not None:   # None means no such attribute
                    w_self.dict_w[attr] = w_value
                    return w_value
        return w_value

    @elidable
    def _pure_getdictvalue_no_unwrapping(w_self, space, version_tag, attr):
        return w_self._getdictvalue_no_unwrapping(space, attr)

    def setdictvalue(w_self, space, name, w_value):
        if not w_self.is_heaptype():
            raise oefmt(space.w_TypeError,
                        "can't set attributes on type object '%N'", w_self)
        if name == "__del__" and name not in w_self.dict_w:
            msg = ("a __del__ method added to an existing type will not be "
                   "called")
            space.warn(space.wrap(msg), space.w_RuntimeWarning)
        if space.config.objspace.std.withtypeversion:
            version_tag = w_self.version_tag()
            if version_tag is not None:
                w_curr = w_self._pure_getdictvalue_no_unwrapping(
                        space, version_tag, name)
                w_value = write_cell(space, w_curr, w_value)
                if w_value is None:
                    return True
        w_self.mutated(name)
        w_self.dict_w[name] = w_value
        return True

    def deldictvalue(w_self, space, key):
        if w_self.lazyloaders:
            w_self._cleanup_()    # force un-lazification
        if not w_self.is_heaptype():
            raise oefmt(space.w_TypeError,
                        "can't delete attributes on type object '%N'", w_self)
        try:
            del w_self.dict_w[key]
        except KeyError:
            return False
        else:
            w_self.mutated(key)
            return True

    def lookup(w_self, name):
        # note that this doesn't call __get__ on the result at all
        space = w_self.space
        if space.config.objspace.std.withmethodcache:
            return w_self.lookup_where_with_method_cache(name)[1]

        return w_self._lookup(name)

    def lookup_where(w_self, name):
        space = w_self.space
        if space.config.objspace.std.withmethodcache:
            return w_self.lookup_where_with_method_cache(name)

        return w_self._lookup_where(name)

    @unroll_safe
    def lookup_starting_at(w_self, w_starttype, name):
        space = w_self.space
        look = False
        for w_class in w_self.mro_w:
            if w_class is w_starttype:
                look = True
            elif look:
                w_value = w_class.getdictvalue(space, name)
                if w_value is not None:
                    return w_value
        return None

    @unroll_safe
    def _lookup(w_self, key):
        space = w_self.space
        for w_class in w_self.mro_w:
            w_value = w_class.getdictvalue(space, key)
            if w_value is not None:
                return w_value
        return None

    @unroll_safe
    def _lookup_where(w_self, key):
        # like lookup() but also returns the parent class in which the
        # attribute was found
        space = w_self.space
        for w_class in w_self.mro_w:
            w_value = w_class.getdictvalue(space, key)
            if w_value is not None:
                return w_class, w_value
        return None, None

    def _lookup_where_all_typeobjects(w_self, key):
        # like _lookup_where(), but when we know that w_self.mro_w only
        # contains W_TypeObjects.  (It differs from _lookup_where() mostly
        # from a JIT point of view: it cannot invoke arbitrary Python code.)
        space = w_self.space
        for w_class in w_self.mro_w:
            assert isinstance(w_class, W_TypeObject)
            w_value = w_class._getdictvalue_no_unwrapping(space, key)
            if w_value is not None:
                return w_class, w_value
        return None, None

    def lookup_where_with_method_cache(w_self, name):
        space = w_self.space
        promote(w_self)
        assert space.config.objspace.std.withmethodcache
        version_tag = promote(w_self.version_tag())
        if version_tag is None:
            tup = w_self._lookup_where(name)
            return tup
        tup_w = w_self._pure_lookup_where_with_method_cache(name, version_tag)
        w_class, w_value = tup_w
        if (space.config.objspace.std.withtypeversion and
                isinstance(w_value, MutableCell)):
            return w_class, w_value.unwrap_cell(space)
        return tup_w   # don't make a new tuple, reuse the old one

    def _pure_lookup_where_possibly_with_method_cache(w_self, name, version_tag):
        if w_self.space.config.objspace.std.withmethodcache:
            return w_self._pure_lookup_where_with_method_cache(name, version_tag)
        else:
            return w_self._lookup_where_all_typeobjects(name)

    @elidable
    def _pure_lookup_where_with_method_cache(w_self, name, version_tag):
        space = w_self.space
        cache = space.fromcache(MethodCache)
        SHIFT2 = r_uint.BITS - space.config.objspace.std.methodcachesizeexp
        SHIFT1 = SHIFT2 - 5
        version_tag_as_int = current_object_addr_as_int(version_tag)
        # ^^^Note: if the version_tag object is moved by a moving GC, the
        # existing method cache entries won't be found any more; new
        # entries will be created based on the new address.  The
        # assumption is that the version_tag object won't keep moving all
        # the time - so using the fast current_object_addr_as_int() instead
        # of a slower solution like hash() is still a good trade-off.
        hash_name = compute_hash(name)
        product = intmask(version_tag_as_int * hash_name)
        method_hash = (r_uint(product) ^ (r_uint(product) << SHIFT1)) >> SHIFT2
        # ^^^Note2: we used to just take product>>SHIFT2, but on 64-bit
        # platforms SHIFT2 is really large, and we loose too much information
        # that way (as shown by failures of the tests that typically have
        # method names like 'f' who hash to a number that has only ~33 bits).
        cached_version_tag = cache.versions[method_hash]
        if cached_version_tag is version_tag:
            cached_name = cache.names[method_hash]
            if cached_name is name:
                tup = cache.lookup_where[method_hash]
                if space.config.objspace.std.withmethodcachecounter:
                    cache.hits[name] = cache.hits.get(name, 0) + 1
#                print "hit", w_self, name
                return tup
        tup = w_self._lookup_where_all_typeobjects(name)
        cache.versions[method_hash] = version_tag
        cache.names[method_hash] = name
        cache.lookup_where[method_hash] = tup
        if space.config.objspace.std.withmethodcachecounter:
            cache.misses[name] = cache.misses.get(name, 0) + 1
#        print "miss", w_self, name
        return tup

    def check_user_subclass(w_self, w_subtype):
        space = w_self.space
        if not isinstance(w_subtype, W_TypeObject):
            raise oefmt(space.w_TypeError,
                        "X is not a type object ('%T')", w_subtype)
        if not w_subtype.issubtype(w_self):
            raise oefmt(space.w_TypeError,
                        "%N.__new__(%N): %N is not a subtype of %N",
                        w_self, w_subtype, w_subtype, w_self)
        if w_self.layout.typedef is not w_subtype.layout.typedef:
            raise oefmt(space.w_TypeError,
                        "%N.__new__(%N) is not safe, use %N.__new__()",
                        w_self, w_subtype, w_subtype)
        return w_subtype

    def _cleanup_(w_self):
        "NOT_RPYTHON.  Forces the lazy attributes to be computed."
        if 'lazyloaders' in w_self.__dict__:
            for attr in w_self.lazyloaders.keys():
                w_self.getdictvalue(w_self.space, attr)
            del w_self.lazyloaders

    def getdict(w_self, space): # returning a dict-proxy!
        from pypy.objspace.std.dictproxyobject import DictProxyStrategy
        from pypy.objspace.std.dictmultiobject import W_DictObject
        if w_self.lazyloaders:
            w_self._cleanup_()    # force un-lazification
        strategy = space.fromcache(DictProxyStrategy)
        storage = strategy.erase(w_self)
        return W_DictObject(space, strategy, storage)

    def is_heaptype(w_self):
        return w_self.flag_heaptype

    def is_cpytype(w_self):
        return w_self.flag_cpytype

    def is_abstract(w_self):
        return w_self.flag_abstract

    def set_abstract(w_self, abstract):
        w_self.flag_abstract = bool(abstract)

    def issubtype(w_self, w_type):
        promote(w_self)
        promote(w_type)
        if w_self.space.config.objspace.std.withtypeversion and we_are_jitted():
            version_tag1 = w_self.version_tag()
            version_tag2 = w_type.version_tag()
            if version_tag1 is not None and version_tag2 is not None:
                res = _pure_issubtype(w_self, w_type, version_tag1, version_tag2)
                return res
        return _issubtype(w_self, w_type)

    def get_module(self):
        space = self.space
        if self.is_heaptype():
            return self.getdictvalue(space, '__module__')
        else:
            dot = self.name.find('.')
            if dot >= 0:
                mod = self.name[:dot]
            else:
                mod = "__builtin__"
            return space.wrap(mod)

    def getname(self, space):
        if self.is_heaptype():
            return self.name
        else:
            dot = self.name.find('.')
            if dot >= 0:
                return self.name[dot+1:]
            else:
                return self.name

    def add_subclass(w_self, w_subclass):
        space = w_self.space
        if not space.config.translation.rweakref:
            return    # no weakref support, don't keep track of subclasses
        import weakref
        assert isinstance(w_subclass, W_TypeObject)
        newref = weakref.ref(w_subclass)
        for i in range(len(w_self.weak_subclasses)):
            ref = w_self.weak_subclasses[i]
            if ref() is None:
                w_self.weak_subclasses[i] = newref
                return
        else:
            w_self.weak_subclasses.append(newref)

    def remove_subclass(w_self, w_subclass):
        space = w_self.space
        if not space.config.translation.rweakref:
            return    # no weakref support, don't keep track of subclasses
        for i in range(len(w_self.weak_subclasses)):
            ref = w_self.weak_subclasses[i]
            if ref() is w_subclass:
                del w_self.weak_subclasses[i]
                return

    def get_subclasses(w_self):
        space = w_self.space
        if not space.config.translation.rweakref:
            raise oefmt(space.w_RuntimeError,
                        "this feature requires weakrefs, "
                        "which are not available in this build of PyPy")
        subclasses_w = []
        for ref in w_self.weak_subclasses:
            w_ob = ref()
            if w_ob is not None:
                subclasses_w.append(w_ob)
        return subclasses_w

    # for now, weakref support for W_TypeObject is hard to get automatically
    _lifeline_ = None

    def getweakref(self):
        return self._lifeline_

    def setweakref(self, space, weakreflifeline):
        self._lifeline_ = weakreflifeline

    def delweakref(self):
        self._lifeline_ = None

    def descr_call(self, space, __args__):
        promote(self)
        # invoke the __new__ of the type
        if not we_are_jitted():
            # note that the annotator will figure out that self.w_new_function
            # can only be None if the newshortcut config option is not set
            w_newfunc = self.w_new_function
        else:
            # for the JIT it is better to take the slow path because normal lookup
            # is nicely optimized, but the self.w_new_function attribute is not
            # known to the JIT
            w_newfunc = None
        if w_newfunc is None:
            w_newtype, w_newdescr = self.lookup_where('__new__')
            if w_newdescr is None:    # see test_crash_mro_without_object_1
                raise oefmt(space.w_TypeError, "cannot create '%N' instances",
                            self)
            w_newfunc = space.get(w_newdescr, self)
            if (space.config.objspace.std.newshortcut and
                not we_are_jitted() and
                isinstance(w_newtype, W_TypeObject)):
                self.w_new_function = w_newfunc
        w_newobject = space.call_obj_args(w_newfunc, self, __args__)
        call_init = space.isinstance_w(w_newobject, self)

        # maybe invoke the __init__ of the type
        if (call_init and not (space.is_w(self, space.w_type) and
            not __args__.keywords and len(__args__.arguments_w) == 1)):
            w_descr = space.lookup(w_newobject, '__init__')
            if w_descr is not None:    # see test_crash_mro_without_object_2
                w_result = space.get_and_call_args(w_descr, w_newobject,
                                                   __args__)
                if not space.is_w(w_result, space.w_None):
                    raise oefmt(space.w_TypeError,
                                "__init__() should return None")
        return w_newobject

    def descr_repr(self, space):
        w_mod = self.get_module()
        if w_mod is None or not space.isinstance_w(w_mod, space.w_str):
            mod = None
        else:
            mod = space.str_w(w_mod)
        if not self.is_heaptype():
            kind = 'type'
        else:
            kind = 'class'
        if mod is not None and mod != '__builtin__':
            return space.wrap("<%s '%s.%s'>" % (kind, mod, self.getname(space)))
        else:
            return space.wrap("<%s '%s'>" % (kind, self.name))

    def descr_getattribute(self, space, w_name):
        name = space.str_w(w_name)
        w_descr = space.lookup(self, name)
        if w_descr is not None:
            if space.is_data_descr(w_descr):
                w_get = space.lookup(w_descr, "__get__")
                if w_get is not None:
                    return space.get_and_call_function(w_get, w_descr, self,
                                                       space.type(self))
        w_value = self.lookup(name)
        if w_value is not None:
            # __get__(None, type): turns e.g. functions into unbound methods
            return space.get(w_value, space.w_None, self)
        if w_descr is not None:
            return space.get(w_descr, self)
        raise oefmt(space.w_AttributeError,
                    "type object '%N' has no attribute %R", self, w_name)

    def descr_eq(self, space, w_other):
        if not isinstance(w_other, W_TypeObject):
            return space.w_NotImplemented
        return space.is_(self, w_other)

    def descr_ne(self, space, w_other):
        if not isinstance(w_other, W_TypeObject):
            return space.w_NotImplemented
        return space.newbool(not space.is_w(self, w_other))


def descr__new__(space, w_typetype, w_name, w_bases=None, w_dict=None):
    "This is used to create user-defined classes only."
    # XXX check types

    w_typetype = _precheck_for_new(space, w_typetype)

    # special case for type(x)
    if (space.is_w(space.type(w_typetype), space.w_type) and w_bases is None and
        w_dict is None):
        return space.type(w_name)
    else:
        return _create_new_type(space, w_typetype, w_name, w_bases, w_dict)


def _create_new_type(space, w_typetype, w_name, w_bases, w_dict):
    # this is in its own function because we want the special case 'type(x)'
    # above to be seen by the jit.
    if w_bases is None or w_dict is None:
        raise oefmt(space.w_TypeError, "type() takes 1 or 3 arguments")

    bases_w = space.fixedview(w_bases)

    w_winner = w_typetype
    for base in bases_w:
        w_typ = space.type(base)
        if space.is_w(w_typ, space.w_classobj):
            continue # special-case old-style classes
        if space.is_true(space.issubtype(w_winner, w_typ)):
            continue
        if space.is_true(space.issubtype(w_typ, w_winner)):
            w_winner = w_typ
            continue
        raise oefmt(space.w_TypeError,
                    "metaclass conflict: the metaclass of a derived class must"
                    " be a (non-strict) subclass of the metaclasses of all its"
                    " bases")

    if not space.is_w(w_winner, w_typetype):
        newfunc = space.getattr(w_winner, space.wrap('__new__'))
        if not space.is_w(newfunc, space.getattr(space.w_type, space.wrap('__new__'))):
            return space.call_function(newfunc, w_winner, w_name, w_bases, w_dict)
        w_typetype = w_winner

    name = space.str_w(w_name)
    assert isinstance(name, str)
    dict_w = {}
    dictkeys_w = space.listview(w_dict)
    for w_key in dictkeys_w:
        key = space.str_w(w_key)
        dict_w[key] = space.getitem(w_dict, w_key)
    w_type = space.allocate_instance(W_TypeObject, w_typetype)
    W_TypeObject.__init__(w_type, space, name, bases_w or [space.w_object],
                          dict_w)
    w_type.ready()
    return w_type

def _precheck_for_new(space, w_type):
    if not isinstance(w_type, W_TypeObject):
        raise oefmt(space.w_TypeError, "X is not a type object (%T)", w_type)
    return w_type

# ____________________________________________________________

def _check(space, w_type, msg="descriptor is for 'type'"):
    if not isinstance(w_type, W_TypeObject):
        raise OperationError(space.w_TypeError, space.wrap(msg))
    return w_type


def descr_get__name__(space, w_type):
    w_type = _check(space, w_type)
    return space.wrap(w_type.getname(space))

def descr_set__name__(space, w_type, w_value):
    w_type = _check(space, w_type)
    if not w_type.is_heaptype():
        raise oefmt(space.w_TypeError, "can't set %N.__name__", w_type)
    name = space.str_w(w_value)
    if '\x00' in name:
        raise oefmt(space.w_ValueError, "__name__ must not contain null bytes")
    w_type.name = name

def descr_get__mro__(space, w_type):
    w_type = _check(space, w_type)
    return space.newtuple(w_type.mro_w)

def descr_mro(space, w_type):
    """Return a type's method resolution order."""
    w_type = _check(space, w_type, "expected type")
    return space.newlist(w_type.compute_default_mro())

def descr_get__bases__(space, w_type):
    w_type = _check(space, w_type)
    return space.newtuple(w_type.bases_w)

def mro_subclasses(space, w_type, temp):
    temp.append((w_type, w_type.mro_w))
    compute_mro(w_type)
    for w_sc in w_type.get_subclasses():
        assert isinstance(w_sc, W_TypeObject)
        mro_subclasses(space, w_sc, temp)

def descr_set__bases__(space, w_type, w_value):
    # this assumes all app-level type objects are W_TypeObject
    w_type = _check(space, w_type)
    if not w_type.is_heaptype():
        raise oefmt(space.w_TypeError, "can't set %N.__bases__", w_type)
    if not space.isinstance_w(w_value, space.w_tuple):
        raise oefmt(space.w_TypeError,
                    "can only assign tuple to %N.__bases__, not %T",
                    w_type, w_value)
    newbases_w = space.fixedview(w_value)
    if len(newbases_w) == 0:
        raise oefmt(space.w_TypeError,
                    "can only assign non-empty tuple to %N.__bases__, not ()",
                    w_type)

    for w_newbase in newbases_w:
        if isinstance(w_newbase, W_TypeObject):
            if w_type in w_newbase.compute_default_mro():
                raise oefmt(space.w_TypeError,
                            "a __bases__ item causes an inheritance cycle")

    w_oldbestbase = check_and_find_best_base(space, w_type.bases_w)
    w_newbestbase = check_and_find_best_base(space, newbases_w)
    oldlayout = w_oldbestbase.get_full_instance_layout()
    newlayout = w_newbestbase.get_full_instance_layout()

    if oldlayout != newlayout:
        raise oefmt(space.w_TypeError,
                    "__bases__ assignment: '%N' object layout differs from "
                    "'%N'", w_newbestbase, w_oldbestbase)

    # invalidate the version_tag of all the current subclasses
    w_type.mutated(None)

    # now we can go ahead and change 'w_type.bases_w'
    saved_bases_w = w_type.bases_w
    temp = []
    try:
        for w_oldbase in saved_bases_w:
            if isinstance(w_oldbase, W_TypeObject):
                w_oldbase.remove_subclass(w_type)
        w_type.bases_w = newbases_w
        for w_newbase in newbases_w:
            if isinstance(w_newbase, W_TypeObject):
                w_newbase.add_subclass(w_type)
        # try to recompute all MROs
        mro_subclasses(space, w_type, temp)
    except:
        for cls, old_mro in temp:
            cls.mro_w = old_mro
        w_type.bases_w = saved_bases_w
        raise
    if (space.config.objspace.std.withtypeversion and
        w_type.version_tag() is not None and
        not is_mro_purely_of_types(w_type.mro_w)):
        # Disable method cache if the hierarchy isn't pure.
        w_type._version_tag = None
        for w_subclass in w_type.get_subclasses():
            if isinstance(w_subclass, W_TypeObject):
                w_subclass._version_tag = None

def descr__base(space, w_type):
    w_type = _check(space, w_type)
    return find_best_base(w_type.bases_w)

def descr__doc(space, w_type):
    if space.is_w(w_type, space.w_type):
        return space.wrap("""type(object) -> the object's type
type(name, bases, dict) -> a new type""")
    w_type = _check(space, w_type)
    if not w_type.is_heaptype():
        return w_type.w_doc
    w_result = w_type.getdictvalue(space, '__doc__')
    if w_result is None:
        return space.w_None
    else:
        return space.get(w_result, space.w_None, w_type)

def descr__flags(space, w_type):
    from copy_reg import _HEAPTYPE
    _CPYTYPE = 1 # used for non-heap types defined in C
    _ABSTRACT = 1 << 20
    #
    w_type = _check(space, w_type)
    flags = 0
    if w_type.flag_heaptype:
        flags |= _HEAPTYPE
    if w_type.flag_cpytype:
        flags |= _CPYTYPE
    if w_type.flag_abstract:
        flags |= _ABSTRACT
    return space.wrap(flags)

def descr_get__module(space, w_type):
    w_type = _check(space, w_type)
    return w_type.get_module()

def descr_set__module(space, w_type, w_value):
    w_type = _check(space, w_type)
    w_type.setdictvalue(space, '__module__', w_value)

def descr_get___abstractmethods__(space, w_type):
    w_type = _check(space, w_type)
    # type itself has an __abstractmethods__ descriptor (this). Don't return it
    if not space.is_w(w_type, space.w_type):
        w_result = w_type.getdictvalue(space, "__abstractmethods__")
        if w_result is not None:
            return w_result
    raise oefmt(space.w_AttributeError, "__abstractmethods__")

def descr_set___abstractmethods__(space, w_type, w_new):
    w_type = _check(space, w_type)
    w_type.setdictvalue(space, "__abstractmethods__", w_new)
    w_type.set_abstract(space.is_true(w_new))

def descr_del___abstractmethods__(space, w_type):
    w_type = _check(space, w_type)
    if not w_type.deldictvalue(space, "__abstractmethods__"):
        raise oefmt(space.w_AttributeError, "__abstractmethods__")
    w_type.set_abstract(False)

def descr___subclasses__(space, w_type):
    """Return the list of immediate subclasses."""
    w_type = _check(space, w_type)
    return space.newlist(w_type.get_subclasses())

# ____________________________________________________________

@gateway.unwrap_spec(w_obj=W_TypeObject, w_sub=W_TypeObject)
def type_issubtype(w_obj, space, w_sub):
    return space.newbool(w_sub.issubtype(w_obj))

@gateway.unwrap_spec(w_obj=W_TypeObject)
def type_isinstance(w_obj, space, w_inst):
    return space.newbool(space.type(w_inst).issubtype(w_obj))

W_TypeObject.typedef = TypeDef("type",
    __new__ = gateway.interp2app(descr__new__),
    __name__ = GetSetProperty(descr_get__name__, descr_set__name__),
    __bases__ = GetSetProperty(descr_get__bases__, descr_set__bases__),
    __base__ = GetSetProperty(descr__base),
    __mro__ = GetSetProperty(descr_get__mro__),
    __dict__ = GetSetProperty(descr_get_dict),
    __doc__ = GetSetProperty(descr__doc),
    mro = gateway.interp2app(descr_mro),
    __flags__ = GetSetProperty(descr__flags),
    __module__ = GetSetProperty(descr_get__module, descr_set__module),
    __abstractmethods__ = GetSetProperty(descr_get___abstractmethods__,
                                         descr_set___abstractmethods__,
                                         descr_del___abstractmethods__),
    __subclasses__ = gateway.interp2app(descr___subclasses__),
    __weakref__ = weakref_descr,
    __instancecheck__ = gateway.interp2app(type_isinstance),
    __subclasscheck__ = gateway.interp2app(type_issubtype),

    __call__ = gateway.interp2app(W_TypeObject.descr_call),
    __repr__ = gateway.interp2app(W_TypeObject.descr_repr),
    __getattribute__ = gateway.interp2app(W_TypeObject.descr_getattribute),
    __eq__ = gateway.interp2app(W_TypeObject.descr_eq),
    __ne__ = gateway.interp2app(W_TypeObject.descr_ne),
)


# ____________________________________________________________
# Initialization of type objects

def find_best_base(bases_w):
    """The best base is one of the bases in the given list: the one
       whose layout a new type should use as a starting point.
    """
    w_bestbase = None
    for w_candidate in bases_w:
        if not isinstance(w_candidate, W_TypeObject):
            continue
        if w_bestbase is None:
            w_bestbase = w_candidate   # for now
            continue
        cand_layout = w_candidate.layout
        best_layout = w_bestbase.layout
        if (cand_layout is not best_layout and
            cand_layout.issublayout(best_layout)):
            w_bestbase = w_candidate
    return w_bestbase

def check_and_find_best_base(space, bases_w):
    """The best base is one of the bases in the given list: the one
       whose layout a new type should use as a starting point.
       This version checks that bases_w is an acceptable tuple of bases.
    """
    w_bestbase = find_best_base(bases_w)
    if w_bestbase is None:
        raise oefmt(space.w_TypeError,
                    "a new-style class can't have only classic bases")
    if not w_bestbase.layout.typedef.acceptable_as_base_class:
        raise oefmt(space.w_TypeError,
                    "type '%N' is not an acceptable base class", w_bestbase)

    # check that all other bases' layouts are "super-layouts" of the
    # bestbase's layout
    best_layout = w_bestbase.layout
    for w_base in bases_w:
        if isinstance(w_base, W_TypeObject):
            layout = w_base.layout
            if not best_layout.issublayout(layout):
                raise oefmt(space.w_TypeError,
                            "instance layout conflicts in multiple inheritance")
    return w_bestbase

def copy_flags_from_bases(w_self, w_bestbase):
    hasoldstylebase = False
    for w_base in w_self.bases_w:
        if not isinstance(w_base, W_TypeObject):
            hasoldstylebase = True
            continue
        w_self.hasdict = w_self.hasdict or w_base.hasdict
        w_self.needsdel = w_self.needsdel or w_base.needsdel
        w_self.weakrefable = w_self.weakrefable or w_base.weakrefable
    return hasoldstylebase

def create_all_slots(w_self, hasoldstylebase, w_bestbase, force_new_layout):
    base_layout = w_bestbase.layout
    index_next_extra_slot = base_layout.nslots
    space = w_self.space
    dict_w = w_self.dict_w
    if '__slots__' not in dict_w:
        wantdict = True
        wantweakref = True
    else:
        wantdict = False
        wantweakref = False
        w_slots = dict_w['__slots__']
        if (space.isinstance_w(w_slots, space.w_str) or
            space.isinstance_w(w_slots, space.w_unicode)):
            slot_names_w = [w_slots]
        else:
            slot_names_w = space.unpackiterable(w_slots)
        for w_slot_name in slot_names_w:
            slot_name = space.str_w(w_slot_name)
            if slot_name == '__dict__':
                if wantdict or w_bestbase.hasdict:
                    raise oefmt(space.w_TypeError,
                                "__dict__ slot disallowed: we already got one")
                wantdict = True
            elif slot_name == '__weakref__':
                if wantweakref or w_bestbase.weakrefable:
                    raise oefmt(space.w_TypeError,
                                "__weakref__ slot disallowed: we already got one")
                wantweakref = True
            else:
                index_next_extra_slot = create_slot(w_self, slot_name,
                                                    index_next_extra_slot)
    wantdict = wantdict or hasoldstylebase
    if wantdict:
        create_dict_slot(w_self)
    if wantweakref:
        create_weakref_slot(w_self)
    if '__del__' in dict_w:
        w_self.needsdel = True
    #
    if index_next_extra_slot == base_layout.nslots and not force_new_layout:
        return base_layout
    else:
        return Layout(base_layout.typedef, index_next_extra_slot,
                      base_layout=base_layout)

def create_slot(w_self, slot_name, index_next_extra_slot):
    space = w_self.space
    if not valid_slot_name(slot_name):
        raise oefmt(space.w_TypeError, "__slots__ must be identifiers")
    # create member
    slot_name = mangle(slot_name, w_self.name)
    if slot_name not in w_self.dict_w:
        # Force interning of slot names.
        slot_name = space.str_w(space.new_interned_str(slot_name))
        # in cpython it is ignored less, but we probably don't care
        member = Member(index_next_extra_slot, slot_name, w_self)
        index_next_extra_slot += 1
        w_self.dict_w[slot_name] = space.wrap(member)
    return index_next_extra_slot

def create_dict_slot(w_self):
    if not w_self.hasdict:
        w_self.dict_w.setdefault('__dict__',
                                 w_self.space.wrap(dict_descr))
        w_self.hasdict = True

def create_weakref_slot(w_self):
    if not w_self.weakrefable:
        w_self.dict_w.setdefault('__weakref__',
                                 w_self.space.wrap(weakref_descr))
        w_self.weakrefable = True

def valid_slot_name(slot_name):
    if len(slot_name) == 0 or slot_name[0].isdigit():
        return False
    for c in slot_name:
        if not c.isalnum() and c != '_':
            return False
    return True

def setup_user_defined_type(w_self, force_new_layout):
    if len(w_self.bases_w) == 0:
        w_self.bases_w = [w_self.space.w_object]
    w_bestbase = check_and_find_best_base(w_self.space, w_self.bases_w)
    w_self.flag_heaptype = True
    for w_base in w_self.bases_w:
        if not isinstance(w_base, W_TypeObject):
            continue
        w_self.flag_cpytype |= w_base.flag_cpytype
        w_self.flag_abstract |= w_base.flag_abstract

    hasoldstylebase = copy_flags_from_bases(w_self, w_bestbase)
    layout = create_all_slots(w_self, hasoldstylebase, w_bestbase,
                              force_new_layout)

    ensure_common_attributes(w_self)
    return layout

def setup_builtin_type(w_self, instancetypedef):
    w_self.hasdict = instancetypedef.hasdict
    w_self.weakrefable = instancetypedef.weakrefable
    w_self.w_doc = w_self.space.wrap(instancetypedef.doc)
    ensure_common_attributes(w_self)
    w_self.flag_heaptype = instancetypedef.heaptype
    #
    # usually 'instancetypedef' is new, i.e. not seen in any base,
    # but not always (see Exception class)
    w_bestbase = find_best_base(w_self.bases_w)
    if w_bestbase is None:
        parent_layout = None
    else:
        parent_layout = w_bestbase.layout
        if parent_layout.typedef is instancetypedef:
            return parent_layout
    return Layout(instancetypedef, 0, base_layout=parent_layout)

def ensure_common_attributes(w_self):
    ensure_static_new(w_self)
    w_self.dict_w.setdefault('__doc__', w_self.w_doc)
    if w_self.is_heaptype():
        ensure_module_attr(w_self)
    w_self.mro_w = []      # temporarily
    compute_mro(w_self)

def ensure_static_new(w_self):
    # special-case __new__, as in CPython:
    # if it is a Function, turn it into a static method
    if '__new__' in w_self.dict_w:
        w_new = w_self.dict_w['__new__']
        if isinstance(w_new, Function):
            w_self.dict_w['__new__'] = StaticMethod(w_new)

def ensure_module_attr(w_self):
    # initialize __module__ in the dict (user-defined types only)
    if '__module__' not in w_self.dict_w:
        space = w_self.space
        caller = space.getexecutioncontext().gettopframe_nohidden()
        if caller is not None:
            w_globals = caller.get_w_globals()
            w_name = space.finditem(w_globals, space.wrap('__name__'))
            if w_name is not None:
                w_self.dict_w['__module__'] = w_name

def compute_mro(w_self):
    if w_self.is_heaptype():
        space = w_self.space
        w_metaclass = space.type(w_self)
        w_where, w_mro_func = space.lookup_in_type_where(w_metaclass, 'mro')
        if w_mro_func is not None and not space.is_w(w_where, space.w_type):
            w_mro_meth = space.get(w_mro_func, w_self)
            w_mro = space.call_function(w_mro_meth)
            mro_w = space.fixedview(w_mro)
            w_self.mro_w = validate_custom_mro(space, mro_w)
            return    # done
    w_self.mro_w = w_self.compute_default_mro()[:]

def validate_custom_mro(space, mro_w):
    # do some checking here.  Note that unlike CPython, strange MROs
    # cannot really segfault PyPy.  At a minimum, we check that all
    # the elements in the mro seem to be (old- or new-style) classes.
    for w_class in mro_w:
        if not space.abstract_isclass_w(w_class):
            raise oefmt(space.w_TypeError, "mro() returned a non-class")
    return mro_w

def is_mro_purely_of_types(mro_w):
    for w_class in mro_w:
        if not isinstance(w_class, W_TypeObject):
            return False
    return True

# ____________________________________________________________

def _issubtype(w_sub, w_type):
    return w_type in w_sub.mro_w

@elidable_promote()
def _pure_issubtype(w_sub, w_type, version_tag1, version_tag2):
    return _issubtype(w_sub, w_type)


# ____________________________________________________________


abstract_mro = gateway.applevel("""
    def abstract_mro(klass):
        # abstract/classic mro
        mro = []
        stack = [klass]
        while stack:
            klass = stack.pop()
            if klass not in mro:
                mro.append(klass)
                if not isinstance(klass.__bases__, tuple):
                    raise TypeError, '__bases__ must be a tuple'
                stack += klass.__bases__[::-1]
        return mro
""", filename=__file__).interphook("abstract_mro")

def get_mro(space, klass):
    if isinstance(klass, W_TypeObject):
        return list(klass.mro_w)
    else:
        return space.unpackiterable(abstract_mro(space, klass))


def compute_C3_mro(space, cls):
    order = []
    orderlists = [get_mro(space, base) for base in cls.bases_w]
    orderlists.append([cls] + cls.bases_w)
    while orderlists:
        for candidatelist in orderlists:
            candidate = candidatelist[0]
            if mro_blockinglist(candidate, orderlists) is None:
                break    # good candidate
        else:
            return mro_error(space, orderlists)  # no candidate found
        assert candidate not in order
        order.append(candidate)
        for i in range(len(orderlists) - 1, -1, -1):
            if orderlists[i][0] is candidate:
                del orderlists[i][0]
                if len(orderlists[i]) == 0:
                    del orderlists[i]
    return order


def mro_blockinglist(candidate, orderlists):
    for lst in orderlists:
        if candidate in lst[1:]:
            return lst
    return None # good candidate

def mro_error(space, orderlists):
    cycle = []
    candidate = orderlists[-1][0]
    if candidate in orderlists[-1][1:]:
        # explicit error message for this specific case
        raise oefmt(space.w_TypeError, "duplicate base class '%N'", candidate)
    while candidate not in cycle:
        cycle.append(candidate)
        nextblockinglist = mro_blockinglist(candidate, orderlists)
        candidate = nextblockinglist[0]
    del cycle[:cycle.index(candidate)]
    cycle.append(candidate)
    cycle.reverse()
    names = [cls.getname(space) for cls in cycle]
    raise OperationError(space.w_TypeError, space.wrap(
        "cycle among base classes: " + ' < '.join(names)))


class TypeCache(SpaceCache):
    def build(self, typedef):
        "NOT_RPYTHON: initialization-time only."
        from pypy.objspace.std.objectobject import W_ObjectObject

        space = self.space
        rawdict = typedef.rawdict
        lazyloaders = {}

        # compute the bases
        if typedef is W_ObjectObject.typedef:
            bases_w = []
        else:
            bases = typedef.bases or [W_ObjectObject.typedef]
            bases_w = [space.gettypeobject(base) for base in bases]

        # wrap everything
        dict_w = {}
        for descrname, descrvalue in rawdict.items():
            dict_w[descrname] = space.wrap(descrvalue)

        if typedef.applevel_subclasses_base is not None:
            overridetypedef = typedef.applevel_subclasses_base.typedef
        else:
            overridetypedef = typedef
        w_type = W_TypeObject(space, typedef.name, bases_w, dict_w,
                              overridetypedef=overridetypedef)
        if typedef is not overridetypedef:
            w_type.w_doc = space.wrap(typedef.doc)
        if hasattr(typedef, 'flag_sequence_bug_compat'):
            w_type.flag_sequence_bug_compat = typedef.flag_sequence_bug_compat
        w_type.lazyloaders = lazyloaders
        return w_type

    def ready(self, w_type):
        w_type.ready()

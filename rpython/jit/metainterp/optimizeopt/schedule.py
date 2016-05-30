from rpython.jit.metainterp.history import (VECTOR, FLOAT, INT,
        ConstInt, ConstFloat, TargetToken)
from rpython.jit.metainterp.resoperation import (rop, ResOperation,
        GuardResOp, VecOperation, OpHelpers, VecOperationNew,
        VectorizationInfo)
from rpython.jit.metainterp.optimizeopt.dependency import (DependencyGraph,
        MemoryRef, Node, IndexVar)
from rpython.jit.metainterp.optimizeopt.renamer import Renamer
from rpython.jit.metainterp.resume import AccumInfo
from rpython.rlib.objectmodel import we_are_translated
from rpython.jit.metainterp.jitexc import NotAProfitableLoop
from rpython.rlib.objectmodel import specialize, always_inline
from rpython.jit.metainterp.jitexc import NotAVectorizeableLoop, NotAProfitableLoop
from rpython.rtyper.lltypesystem.lloperation import llop
from rpython.rtyper.lltypesystem import lltype


def forwarded_vecinfo(op):
    fwd = op.get_forwarded()
    if fwd is None or not isinstance(fwd, VectorizationInfo):
        # the optimizer clears getforwarded AFTER
        # vectorization, it happens that this is not clean
        fwd = VectorizationInfo(op)
        if not op.is_constant():
            op.set_forwarded(fwd)
    return fwd

class SchedulerState(object):
    def __init__(self, graph):
        self.renamer = Renamer()
        self.graph = graph
        self.oplist = []
        self.worklist = []
        self.invariant_oplist = []
        self.invariant_vector_vars = []
        self.seen = {}

    def post_schedule(self):
        loop = self.graph.loop
        self.renamer.rename(loop.jump)
        self.ensure_args_unpacked(loop.jump)
        loop.operations = self.oplist
        loop.prefix = self.invariant_oplist
        if len(self.invariant_vector_vars) + len(self.invariant_oplist) > 0:
            # label
            args = loop.label.getarglist_copy() + self.invariant_vector_vars
            opnum = loop.label.getopnum()
            op = loop.label.copy_and_change(opnum, args)
            self.renamer.rename(op)
            loop.prefix_label = op
            # jump
            args = loop.jump.getarglist_copy() + self.invariant_vector_vars
            opnum = loop.jump.getopnum()
            op = loop.jump.copy_and_change(opnum, args)
            self.renamer.rename(op)
            loop.jump = op

    def profitable(self):
        return True

    def prepare(self):
        for node in self.graph.nodes:
            if node.depends_count() == 0:
                self.worklist.insert(0, node)

    def emit(self, node, scheduler):
        # implement me in subclass. e.g. as in VecScheduleState
        return False

    def delay(self, node):
        return False

    def has_more(self):
        return len(self.worklist) > 0

    def ensure_args_unpacked(self, op):
        pass

    def post_emit(self, node):
        pass

    def pre_emit(self, node):
        pass

class Scheduler(object):
    """ Create an instance of this class to (re)schedule a vector trace. """
    def __init__(self):
        pass

    def next(self, state):
        """ select the next candidate node to be emitted, or None """
        worklist = state.worklist
        visited = 0
        while len(worklist) > 0:
            if visited == len(worklist):
                return None
            node = worklist.pop()
            if node.emitted:
                continue
            if not self.delay(node, state):
                return node
            worklist.insert(0, node)
            visited += 1
        return None

    def try_to_trash_pack(self, state):
        # one element a pack has several dependencies pointing to
        # it thus we MUST skip this pack!
        if len(state.worklist) > 0:
            # break the first!
            i = 0
            node = state.worklist[i]
            i += 1
            while i < len(state.worklist) and not node.pack:
                node = state.worklist[i]
                i += 1

            if not node.pack:
                return False

            pack = node.pack
            for n in node.pack.operations:
                if n.depends_count() > 0:
                    pack.clear()
                    return True
            else:
                return False

        return False

    def delay(self, node, state):
        """ Delay this operation?
            Only if any dependency has not been resolved """
        if state.delay(node):
            return True
        return node.depends_count() != 0

    def mark_emitted(self, node, state, unpack=True):
        """ An operation has been emitted, adds new operations to the worklist
            whenever their dependency count drops to zero.
            Keeps worklist sorted (see priority) """
        worklist = state.worklist
        provides = node.provides()[:]
        for dep in provides: # COPY
            target = dep.to
            node.remove_edge_to(target)
            if not target.emitted and target.depends_count() == 0:
                # sorts them by priority
                i = len(worklist)-1
                while i >= 0:
                    cur = worklist[i]
                    c = (cur.priority - target.priority)
                    if c < 0: # meaning itnode.priority < target.priority:
                        worklist.insert(i+1, target)
                        break
                    elif c == 0:
                        # if they have the same priority, sort them
                        # using the original position in the trace
                        if target.getindex() < cur.getindex():
                            worklist.insert(i+1, target)
                            break
                    i -= 1
                else:
                    worklist.insert(0, target)
        node.clear_dependencies()
        node.emitted = True
        if not node.is_imaginary():
            op = node.getoperation()
            state.renamer.rename(op)
            if unpack:
                state.ensure_args_unpacked(op)
            state.post_emit(node)

    def walk_and_emit(self, state):
        """ Emit all the operations into the oplist parameter.
            Initiates the scheduling. """
        assert isinstance(state, SchedulerState)
        while state.has_more():
            node = self.next(state)
            if node:
                if not state.emit(node, self):
                    if not node.emitted:
                        state.pre_emit(node)
                        self.mark_emitted(node, state)
                        if not node.is_imaginary():
                            op = node.getoperation()
                            state.seen[op] = None
                            state.oplist.append(op)
                continue

            # it happens that packs can emit many nodes that have been
            # added to the scheuldable_nodes list, in this case it could
            # be that no next exists even though the list contains elements
            if not state.has_more():
                break

            if self.try_to_trash_pack(state):
                continue

            raise AssertionError("schedule failed cannot continue. possible reason: cycle")

        if not we_are_translated():
            for node in state.graph.nodes:
                assert node.emitted

def failnbail_transformation(msg):
    msg = '%s\n' % msg
    if we_are_translated():
        llop.debug_print(lltype.Void, msg)
    else:
        import pdb; pdb.set_trace()
    raise NotImplementedError(msg)

class TypeRestrict(object):
    ANY_TYPE = '\x00'
    ANY_SIZE = -1
    ANY_SIGN = -1
    ANY_COUNT = -1
    SIGNED = 1
    UNSIGNED = 0

    def __init__(self,
                 type=ANY_TYPE,
                 bytesize=ANY_SIZE,
                 count=ANY_SIGN,
                 sign=ANY_COUNT):
        self.type = type
        self.bytesize = bytesize
        self.sign = sign
        self.count = count

    @always_inline
    def any_size(self):
        return self.bytesize == TypeRestrict.ANY_SIZE

    @always_inline
    def any_count(self):
        return self.count == TypeRestrict.ANY_COUNT

    def check(self, value):
        vecinfo = forwarded_vecinfo(value)
        assert vecinfo.datatype != '\x00'
        if self.type != TypeRestrict.ANY_TYPE:
            if self.type != vecinfo.datatype:
                msg = "type mismatch %s != %s" % \
                        (self.type, vecinfo.datatype)
                failnbail_transformation(msg)
        assert vecinfo.bytesize > 0
        if not self.any_size():
            if self.bytesize != vecinfo.bytesize:
                msg = "bytesize mismatch %s != %s" % \
                        (self.bytesize, vecinfo.bytesize)
                failnbail_transformation(msg)
        assert vecinfo.count > 0
        if self.count != TypeRestrict.ANY_COUNT:
            if vecinfo.count < self.count:
                msg = "count mismatch %s < %s" % \
                        (self.count, vecinfo.count)
                failnbail_transformation(msg)
        if self.sign != TypeRestrict.ANY_SIGN:
            if bool(self.sign) == vecinfo.sign:
                msg = "sign mismatch %s < %s" % \
                        (self.sign, vecinfo.sign)
                failnbail_transformation(msg)

    def max_input_count(self, count):
        """ How many """
        if self.count != TypeRestrict.ANY_COUNT:
            return self.count
        return count

class OpRestrict(object):
    def __init__(self, argument_restris):
        self.argument_restrictions = argument_restris

    def check_operation(self, state, pack, op):
        pass

    def crop_vector(self, op, newsize, size):
        return newsize, size

    def must_crop_vector(self, op, index):
        restrict = self.argument_restrictions[index]
        vecinfo = forwarded_vecinfo(op.getarg(index))
        size = vecinfo.bytesize
        newsize = self.crop_to_size(op, index)
        return not restrict.any_size() and newsize != size

    @always_inline
    def crop_to_size(self, op, index):
        restrict = self.argument_restrictions[index]
        return restrict.bytesize

    def opcount_filling_vector_register(self, op, vec_reg_size):
        """ How many operations of that kind can one execute
            with a machine instruction of register size X?
        """
        if op.is_typecast():
            if op.casts_down():
                size = op.cast_input_bytesize(vec_reg_size)
                return size // op.cast_from_bytesize()
            else:
                return vec_reg_size // op.cast_to_bytesize()
        vecinfo = forwarded_vecinfo(op)
        return  vec_reg_size // vecinfo.bytesize

class GuardRestrict(OpRestrict):
    def opcount_filling_vector_register(self, op, vec_reg_size):
        arg = op.getarg(0)
        vecinfo = forwarded_vecinfo(arg)
        return vec_reg_size // vecinfo.bytesize

class LoadRestrict(OpRestrict):
    def opcount_filling_vector_register(self, op, vec_reg_size):
        assert rop.is_primitive_load(op.opnum)
        descr = op.getdescr()
        return vec_reg_size // descr.get_item_size_in_bytes()

class StoreRestrict(OpRestrict):
    def __init__(self, argument_restris):
        self.argument_restrictions = argument_restris

    def must_crop_vector(self, op, index):
        vecinfo = forwarded_vecinfo(op.getarg(index))
        bytesize = vecinfo.bytesize
        return self.crop_to_size(op, index) != bytesize

    @always_inline
    def crop_to_size(self, op, index):
        # there is only one parameter that needs to be transformed!
        descr = op.getdescr()
        return descr.get_item_size_in_bytes()

    def opcount_filling_vector_register(self, op, vec_reg_size):
        assert rop.is_primitive_store(op.opnum)
        descr = op.getdescr()
        return vec_reg_size // descr.get_item_size_in_bytes()

class OpMatchSizeTypeFirst(OpRestrict):
    def check_operation(self, state, pack, op):
        i = 0
        infos = [forwarded_vecinfo(o) for o in op.getarglist()]
        arg0 = op.getarg(i)
        while arg0.is_constant() and i < op.numargs():
            i += 1
            arg0 = op.getarg(i)
        vecinfo = forwarded_vecinfo(arg0)
        bytesize = vecinfo.bytesize
        datatype = vecinfo.datatype

        for arg in op.getarglist():
            if arg.is_constant():
                continue
            curvecinfo = forwarded_vecinfo(arg)
            if curvecinfo.bytesize != bytesize:
                raise NotAVectorizeableLoop()
            if curvecinfo.datatype != datatype:
                raise NotAVectorizeableLoop()

class trans(object):

    TR_ANY = TypeRestrict()
    TR_ANY_FLOAT = TypeRestrict(FLOAT)
    TR_ANY_INTEGER = TypeRestrict(INT)
    TR_FLOAT_2 = TypeRestrict(FLOAT, 4, 2)
    TR_DOUBLE_2 = TypeRestrict(FLOAT, 8, 2)
    TR_INT32_2 = TypeRestrict(INT, 4, 2)

    OR_MSTF_I = OpMatchSizeTypeFirst([TR_ANY_INTEGER, TR_ANY_INTEGER])
    OR_MSTF_F = OpMatchSizeTypeFirst([TR_ANY_FLOAT, TR_ANY_FLOAT])
    STORE_RESTRICT = StoreRestrict([None, None, TR_ANY])
    LOAD_RESTRICT = LoadRestrict([])
    GUARD_RESTRICT = GuardRestrict([TR_ANY_INTEGER])

    # note that the following definition is x86 arch specific
    MAPPING = {
        rop.VEC_INT_ADD:            OR_MSTF_I,
        rop.VEC_INT_SUB:            OR_MSTF_I,
        rop.VEC_INT_MUL:            OR_MSTF_I,
        rop.VEC_INT_AND:            OR_MSTF_I,
        rop.VEC_INT_OR:             OR_MSTF_I,
        rop.VEC_INT_XOR:            OR_MSTF_I,
        rop.VEC_INT_EQ:             OR_MSTF_I,
        rop.VEC_INT_NE:             OR_MSTF_I,

        rop.VEC_FLOAT_ADD:          OR_MSTF_F,
        rop.VEC_FLOAT_SUB:          OR_MSTF_F,
        rop.VEC_FLOAT_MUL:          OR_MSTF_F,
        rop.VEC_FLOAT_TRUEDIV:      OR_MSTF_F,
        rop.VEC_FLOAT_ABS:          OpRestrict([TR_ANY_FLOAT]),
        rop.VEC_FLOAT_NEG:          OpRestrict([TR_ANY_FLOAT]),

        rop.VEC_RAW_STORE:          STORE_RESTRICT,
        rop.VEC_SETARRAYITEM_RAW:   STORE_RESTRICT,
        rop.VEC_SETARRAYITEM_GC:    STORE_RESTRICT,

        rop.VEC_RAW_LOAD_I:         LOAD_RESTRICT,
        rop.VEC_RAW_LOAD_F:         LOAD_RESTRICT,
        rop.VEC_GETARRAYITEM_RAW_I: LOAD_RESTRICT,
        rop.VEC_GETARRAYITEM_RAW_F: LOAD_RESTRICT,
        rop.VEC_GETARRAYITEM_GC_I:  LOAD_RESTRICT,
        rop.VEC_GETARRAYITEM_GC_F:  LOAD_RESTRICT,

        rop.VEC_GUARD_TRUE:             GUARD_RESTRICT,
        rop.VEC_GUARD_FALSE:            GUARD_RESTRICT,

        ## irregular
        rop.VEC_INT_SIGNEXT:        OpRestrict([TR_ANY_INTEGER]),

        rop.VEC_CAST_FLOAT_TO_SINGLEFLOAT:  OpRestrict([TR_DOUBLE_2]),
        # weird but the trace will store single floats in int boxes
        rop.VEC_CAST_SINGLEFLOAT_TO_FLOAT:  OpRestrict([TR_INT32_2]),
        rop.VEC_CAST_FLOAT_TO_INT:          OpRestrict([TR_DOUBLE_2]),
        rop.VEC_CAST_INT_TO_FLOAT:          OpRestrict([TR_INT32_2]),

        rop.VEC_FLOAT_EQ:           OpRestrict([TR_ANY_FLOAT,TR_ANY_FLOAT]),
        rop.VEC_FLOAT_NE:           OpRestrict([TR_ANY_FLOAT,TR_ANY_FLOAT]),
        rop.VEC_INT_IS_TRUE:        OpRestrict([TR_ANY_INTEGER,TR_ANY_INTEGER]),
    }

    @staticmethod
    def get(op):
        res = trans.MAPPING.get(op.vector, None)
        if not res:
            failnbail_transformation("could not get OpRestrict for " + str(op))
        return res

def turn_into_vector(state, pack):
    """ Turn a pack into a vector instruction """
    check_if_pack_supported(state, pack)
    state.costmodel.record_pack_savings(pack, pack.numops())
    left = pack.leftmost()
    oprestrict = trans.get(left)
    if oprestrict is not None:
        oprestrict.check_operation(state, pack, left)
    args = left.getarglist_copy()
    prepare_arguments(state, pack, args)
    vecop = VecOperation(left.vector, args, left,
                         pack.numops(), left.getdescr())
    for i,node in enumerate(pack.operations):
        op = node.getoperation()
        if op.returns_void():
            continue
        state.setvector_of_box(op,i,vecop)
        if pack.is_accumulating() and not op.is_guard():
            state.renamer.start_renaming(op, vecop)
    if left.is_guard():
        prepare_fail_arguments(state, pack, left, vecop)
    state.oplist.append(vecop)
    assert vecop.count >= 1

def prepare_arguments(state, pack, args):
    # Transforming one argument to a vector box argument
    # The following cases can occur:
    # 1) argument is present in the box_to_vbox map.
    #    a) vector can be reused immediatly (simple case)
    #    b) the size of the input is mismatching (crop the vector)
    #    c) values are scattered in differnt registers
    #    d) the operand is not at the right position in the vector
    # 2) argument is not known to reside in a vector
    #    a) expand vars/consts before the label and add as argument
    #    b) expand vars created in the loop body
    #
    oprestrict = trans.MAPPING.get(pack.leftmost().vector, None)
    if not oprestrict:
        return
    restrictions = oprestrict.argument_restrictions
    for i,arg in enumerate(args):
        if i >= len(restrictions) or restrictions[i] is None:
            # ignore this argument
            continue
        restrict = restrictions[i]
        if arg.returns_vector():
            restrict.check(arg)
            continue
        pos, vecop = state.getvector_of_box(arg)
        if not vecop:
            # 2) constant/variable expand this box
            expand(state, pack, args, arg, i)
            restrict.check(args[i])
            continue
        # 1)
        args[i] = vecop # a)
        assemble_scattered_values(state, pack, args, i) # c)
        crop_vector(state, oprestrict, restrict, pack, args, i) # b)
        position_values(state, restrict, pack, args, i, pos) # d)
        restrict.check(args[i])

def prepare_fail_arguments(state, pack, left, vecop):
    assert isinstance(left, GuardResOp)
    assert isinstance(vecop, GuardResOp)
    args = left.getfailargs()[:]
    for i, arg in enumerate(args):
        pos, newarg = state.getvector_of_box(arg)
        if newarg is None:
            newarg = arg
        if newarg.is_vector(): # can be moved to guard exit!
            newarg = unpack_from_vector(state, newarg, 0, 1)
        args[i] = newarg
    vecop.setfailargs(args)
    # TODO vecop.rd_snapshot = left.rd_snapshot

@always_inline
def crop_vector(state, oprestrict, restrict, pack, args, i):
    # convert size i64 -> i32, i32 -> i64, ...
    arg = args[i]
    vecinfo = forwarded_vecinfo(arg)
    size = vecinfo.bytesize
    left = pack.leftmost()
    if oprestrict.must_crop_vector(left, i):
        newsize = oprestrict.crop_to_size(left, i)
        assert arg.type == 'i'
        state._prevent_signext(newsize, size)
        count = vecinfo.count
        vecop = VecOperationNew(rop.VEC_INT_SIGNEXT, [arg, ConstInt(newsize)],
                                'i', newsize, vecinfo.signed, count)
        state.oplist.append(vecop)
        state.costmodel.record_cast_int(size, newsize, count)
        args[i] = vecop

@always_inline
def assemble_scattered_values(state, pack, args, index):
    args_at_index = [node.getoperation().getarg(index) for node in pack.operations]
    args_at_index[0] = args[index]
    vectors = pack.argument_vectors(state, pack, index, args_at_index)
    if len(vectors) > 1:
        # the argument is scattered along different vector boxes
        args[index] = gather(state, vectors, pack.numops())
        state.remember_args_in_vector(pack, index, args[index])

@always_inline
def gather(state, vectors, count): # packed < packable and packed < stride:
    (_, arg) = vectors[0]
    i = 1
    while i < len(vectors):
        (newarg_pos, newarg) = vectors[i]
        vecinfo = forwarded_vecinfo(arg)
        newvecinfo = forwarded_vecinfo(newarg)
        if vecinfo.count + newvecinfo.count <= count:
            arg = pack_into_vector(state, arg, vecinfo.count, newarg, newarg_pos, newvecinfo.count)
        i += 1
    return arg

@always_inline
def position_values(state, restrict, pack, args, index, position):
    arg = args[index]
    vecinfo = forwarded_vecinfo(arg)
    count = vecinfo.count
    newcount = restrict.count
    if not restrict.any_count() and newcount != count:
        if position == 0:
            pass
        pass
    if position != 0:
        # The vector box is at a position != 0 but it
        # is required to be at position 0. Unpack it!
        arg = args[index]
        vecinfo = forwarded_vecinfo(arg)
        count = restrict.max_input_count(vecinfo.count)
        args[index] = unpack_from_vector(state, arg, position, count)
        state.remember_args_in_vector(pack, index, args[index])

def check_if_pack_supported(state, pack):
    left = pack.leftmost()
    vecinfo = forwarded_vecinfo(left)
    insize = vecinfo.bytesize
    if left.is_typecast():
        # prohibit the packing of signext calls that
        # cast to int16/int8.
        state._prevent_signext(left.cast_to_bytesize(),
                               left.cast_from_bytesize())
    if left.getopnum() == rop.INT_MUL:
        if insize == 8 or insize == 1:
            # see assembler for comment why
            raise NotAProfitableLoop

def unpack_from_vector(state, arg, index, count):
    """ Extract parts of the vector box into another vector box """
    assert count > 0
    vecinfo = forwarded_vecinfo(arg)
    assert index + count <= vecinfo.count
    args = [arg, ConstInt(index), ConstInt(count)]
    vecop = OpHelpers.create_vec_unpack(arg.type, args, vecinfo.bytesize,
                                        vecinfo.signed, count)
    state.costmodel.record_vector_unpack(arg, index, count)
    state.oplist.append(vecop)
    return vecop

def pack_into_vector(state, tgt, tidx, src, sidx, scount):
    """ tgt = [1,2,3,4,_,_,_,_]
        src = [5,6,_,_]
        new_box = [1,2,3,4,5,6,_,_] after the operation, tidx=4, scount=2
    """
    assert sidx == 0 # restriction
    vecinfo = forwarded_vecinfo(tgt)
    newcount = vecinfo.count + scount
    args = [tgt, src, ConstInt(tidx), ConstInt(scount)]
    vecop = OpHelpers.create_vec_pack(tgt.type, args, vecinfo.bytesize, vecinfo.signed, newcount)
    state.oplist.append(vecop)
    state.costmodel.record_vector_pack(src, sidx, scount)
    if not we_are_translated():
        _check_vec_pack(vecop)
    return vecop

def _check_vec_pack(op):
    arg0 = op.getarg(0)
    arg1 = op.getarg(1)
    index = op.getarg(2)
    count = op.getarg(3)
    assert op.is_vector()
    assert arg0.is_vector()
    assert index.is_constant()
    assert isinstance(count, ConstInt)
    vecinfo = forwarded_vecinfo(op)
    argvecinfo = forwarded_vecinfo(arg0)
    assert argvecinfo.bytesize == vecinfo.bytesize
    if arg1.is_vector():
        assert argvecinfo.bytesize == vecinfo.bytesize
    else:
        assert count.value == 1
    assert index.value < vecinfo.count
    assert index.value + count.value <= vecinfo.count
    assert vecinfo.count > argvecinfo.count

def expand(state, pack, args, arg, index):
    """ Expand a value into a vector box. useful for arith metic
        of one vector with a scalar (either constant/varialbe)
    """
    left = pack.leftmost()
    box_type = arg.type
    expanded_map = state.expanded_map

    ops = state.invariant_oplist
    variables = state.invariant_vector_vars
    if not arg.is_constant() and arg not in state.inputargs:
        # cannot be created before the loop, expand inline
        ops = state.oplist
        variables = None

    for i, node in enumerate(pack.operations):
        op = node.getoperation()
        if not arg.same_box(op.getarg(index)):
            break
        i += 1
    else:
        # note that heterogenous nodes are not yet tracked
        vecop = state.find_expanded([arg])
        if vecop:
            args[index] = vecop
            return vecop
        left = pack.leftmost()
        vecinfo = forwarded_vecinfo(left)
        vecop = OpHelpers.create_vec_expand(arg, vecinfo.bytesize, vecinfo.signed, pack.numops())
        ops.append(vecop)
        if variables is not None:
            variables.append(vecop)
        state.expand([arg], vecop)
        args[index] = vecop
        return vecop

    # quick search if it has already been expanded
    expandargs = [op.getoperation().getarg(index) for op in pack.operations]
    vecop = state.find_expanded(expandargs)
    if vecop:
        args[index] = vecop
        return vecop

    arg_vecinfo = forwarded_vecinfo(arg)
    vecop = OpHelpers.create_vec(arg.type, arg_vecinfo.bytesize, arg_vecinfo.signed, pack.opnum())
    ops.append(vecop)
    for i,node in enumerate(pack.operations):
        op = node.getoperation()
        arg = op.getarg(index)
        arguments = [vecop, arg, ConstInt(i), ConstInt(1)]
        vecinfo = forwarded_vecinfo(vecop)
        vecop = OpHelpers.create_vec_pack(arg.type, arguments, vecinfo.bytesize,
                                          vecinfo.signed, vecinfo.count+1)
        ops.append(vecop)
    state.expand(expandargs, vecop)

    if variables is not None:
        variables.append(vecop)
    args[index] = vecop

class VecScheduleState(SchedulerState):
    def __init__(self, graph, packset, cpu, costmodel):
        SchedulerState.__init__(self, graph)
        self.box_to_vbox = {}
        self.cpu = cpu
        self.vec_reg_size = cpu.vector_register_size
        self.expanded_map = {}
        self.costmodel = costmodel
        self.inputargs = {}
        self.packset = packset
        for arg in graph.loop.inputargs:
            self.inputargs[arg] = None
        self.accumulation = {}

    def expand(self, args, vecop):
        index = 0
        if len(args) == 1:
            # loop is executed once, thus sets -1 as index
            index = -1
        for arg in args:
            self.expanded_map.setdefault(arg, []).append((vecop, index))
            index += 1

    def find_expanded(self, args):
        if len(args) == 1:
            candidates = self.expanded_map.get(args[0], [])
            for (vecop, index) in candidates:
                if index == -1:
                    # found an expanded variable/constant
                    return vecop
            return None
        possible = {}
        for i, arg in enumerate(args):
            expansions = self.expanded_map.get(arg, [])
            candidates = [vecop for (vecop, index) in expansions \
                          if i == index and possible.get(vecop,True)]
            for vecop in candidates:
                for key in possible.keys():
                    if key not in candidates:
                        # delete every not possible key,value
                        possible[key] = False
                # found a candidate, append it if not yet present
                possible[vecop] = True

            if not possible:
                # no possibility left, this combination is not expanded
                return None
        for vecop,valid in possible.items():
            if valid:
                return vecop
        return None

    def post_emit(self, node):
        pass

    def pre_emit(self, node):
        op = node.getoperation()
        if op.is_guard():
            # add accumulation info to the descriptor
            failargs = op.getfailargs()[:]
            descr = op.getdescr()
            # note: stitching a guard must resemble the order of the label
            # otherwise a wrong mapping is handed to the register allocator
            for i,arg in enumerate(failargs):
                if arg is None:
                    continue
                accum = self.accumulation.get(arg, None)
                if accum:
                    from rpython.jit.metainterp.compile import AbstractResumeGuardDescr
                    assert isinstance(accum, AccumPack)
                    assert isinstance(descr, AbstractResumeGuardDescr)
                    info = AccumInfo(i, arg, accum.operator)
                    descr.attach_vector_info(info)
                    seed = accum.getleftmostseed()
                    failargs[i] = self.renamer.rename_map.get(seed, seed)
            op.setfailargs(failargs)

    def profitable(self):
        return self.costmodel.profitable()

    def prepare(self):
        SchedulerState.prepare(self)
        self.packset.accumulate_prepare(self)
        for arg in self.graph.loop.label.getarglist():
            self.seen[arg] = None

    def emit(self, node, scheduler):
        """ If you implement a scheduler this operations is called
            to emit the actual operation into the oplist of the scheduler.
        """
        if node.pack:
            assert node.pack.numops() > 1
            for node in node.pack.operations:
                self.pre_emit(node)
                scheduler.mark_emitted(node, self, unpack=False)
            turn_into_vector(self, node.pack)
            return True
        return False

    def delay(self, node):
        if node.pack:
            pack = node.pack
            if pack.is_accumulating():
                for node in pack.operations:
                    for dep in node.depends():
                        if dep.to.pack is not pack:
                            return True
            else:
                for node in pack.operations:
                    if node.depends_count() > 0:
                        return True
        return False

    def ensure_args_unpacked(self, op):
        """ If a box is needed that is currently stored within a vector
            box, this utility creates a unpacking instruction.
        """
        # unpack for an immediate use
        for i, argument in enumerate(op.getarglist()):
            if not argument.is_constant():
                arg = self.ensure_unpacked(i, argument)
                if argument is not arg:
                    op.setarg(i, arg)
        # unpack for a guard exit
        if op.is_guard():
            # could be moved to the guard exit
            fail_args = op.getfailargs()
            for i, argument in enumerate(fail_args):
                if argument and not argument.is_constant():
                    arg = self.ensure_unpacked(i, argument)
                    if argument is not arg:
                        fail_args[i] = arg
            op.setfailargs(fail_args)

    def ensure_unpacked(self, index, arg):
        if arg in self.seen or arg.is_vector():
            return arg
        (pos, var) = self.getvector_of_box(arg)
        if var:
            if var in self.invariant_vector_vars:
                return arg
            if arg in self.accumulation:
                return arg
            args = [var, ConstInt(pos), ConstInt(1)]
            vecinfo = forwarded_vecinfo(var)
            vecop = OpHelpers.create_vec_unpack(var.type, args, vecinfo.bytesize,
                                                vecinfo.signed, 1)
            self.renamer.start_renaming(arg, vecop)
            self.seen[vecop] = None
            self.costmodel.record_vector_unpack(var, pos, 1)
            self.oplist.append(vecop)
            return vecop
        return arg

    def _prevent_signext(self, outsize, insize):
        if insize != outsize:
            if outsize < 4 or insize < 4:
                raise NotAProfitableLoop

    def getvector_of_box(self, arg):
        return self.box_to_vbox.get(arg, (-1, None))

    def setvector_of_box(self, var, off, vector):
        if var.returns_void():
            assert 0, "not allowed to rename void resop"
        vecinfo = forwarded_vecinfo(vector)
        assert off < vecinfo.count
        assert not var.is_vector()
        self.box_to_vbox[var] = (off, vector)

    def remember_args_in_vector(self, pack, index, box):
        arguments = [op.getoperation().getarg(index) for op in pack.operations]
        for i,arg in enumerate(arguments):
            vecinfo = forwarded_vecinfo(arg)
            if i >= vecinfo.count:
                break
            self.setvector_of_box(arg, i, box)

class Pack(object):
    """ A pack is a set of n statements that are:
        * isomorphic
        * independent
    """
    FULL = 0
    _attrs_ = ('operations', 'accumulator', 'operator', 'position')

    operator = '\x00'
    position = -1
    accumulator = None

    def __init__(self, ops):
        self.operations = ops
        self.update_pack_of_nodes()

    def numops(self):
        return len(self.operations)

    @specialize.arg(1)
    def leftmost(self, node=False):
        if node:
            return self.operations[0]
        return self.operations[0].getoperation()

    @specialize.arg(1)
    def rightmost(self, node=False):
        if node:
            return self.operations[-1]
        return self.operations[-1].getoperation()

    def pack_type(self):
        ptype = self.input_type
        if self.input_type is None:
            # load does not have an input type, but only an output type
            ptype = self.output_type
        return ptype

    def input_byte_size(self):
        """ The amount of bytes the operations need with the current
            entries in self.operations. E.g. cast_singlefloat_to_float
            takes only #2 operations.
        """
        return self._byte_size(self.input_type)

    def output_byte_size(self):
        """ The amount of bytes the operations need with the current
            entries in self.operations. E.g. vec_load(..., descr=short) 
            with 10 operations returns 20
        """
        return self._byte_size(self.output_type)

    def pack_load(self, vec_reg_size):
        """ Returns the load of the pack a vector register would hold
            just after executing the operation.
            returns: < 0 - empty, nearly empty
                     = 0 - full
                     > 0 - overloaded
        """
        left = self.leftmost()
        if left.returns_void():
            if rop.is_primitive_store(left.opnum):
                # make this case more general if it turns out this is
                # not the only case where packs need to be trashed
                descr = left.getdescr()
                bytesize = descr.get_item_size_in_bytes()
                return bytesize * self.numops() - vec_reg_size
            else:
                assert left.is_guard() and left.getopnum() in \
                       (rop.GUARD_TRUE, rop.GUARD_FALSE)
                vecinfo = forwarded_vecinfo(left.getarg(0))
                bytesize = vecinfo.bytesize
                return bytesize * self.numops() - vec_reg_size
            return 0
        if self.numops() == 0:
            return -1
        if left.is_typecast():
            # casting is special, often only takes a half full vector
            if left.casts_down():
                # size is reduced
                size = left.cast_input_bytesize(vec_reg_size)
                return left.cast_from_bytesize() * self.numops() - size
            else:
                # size is increased
                #size = left.cast_input_bytesize(vec_reg_size)
                return left.cast_to_bytesize() * self.numops() - vec_reg_size
        vecinfo = forwarded_vecinfo(left)
        return vecinfo.bytesize * self.numops() - vec_reg_size

    def is_full(self, vec_reg_size):
        """ If one input element times the opcount is equal
            to the vector register size, we are full!
        """
        return self.pack_load(vec_reg_size) == Pack.FULL

    def opnum(self):
        assert len(self.operations) > 0
        return self.operations[0].getoperation().getopnum()

    def clear(self):
        for node in self.operations:
            node.pack = None
            node.pack_position = -1

    def update_pack_of_nodes(self):
        for i,node in enumerate(self.operations):
            node.pack = self
            node.pack_position = i

    def split(self, packlist, vec_reg_size):
        """ Combination phase creates the biggest packs that are possible.
            In this step the pack is reduced in size to fit into an
            vector register.
        """
        before_count = len(packlist)
        pack = self
        while pack.pack_load(vec_reg_size) > Pack.FULL:
            pack.clear()
            oplist, newoplist = pack.slice_operations(vec_reg_size)
            pack.operations = oplist
            pack.update_pack_of_nodes()
            if not pack.leftmost().is_typecast():
                assert pack.is_full(vec_reg_size)
            #
            newpack = pack.clone(newoplist)
            load = newpack.pack_load(vec_reg_size)
            if load >= Pack.FULL:
                pack.update_pack_of_nodes()
                pack = newpack
                packlist.append(newpack)
            else:
                newpack.clear()
                newpack.operations = []
                break
        pack.update_pack_of_nodes()

    def opcount_filling_vector_register(self, vec_reg_size):
        left = self.leftmost()
        oprestrict = trans.get(left)
        return oprestrict.opcount_filling_vector_register(left, vec_reg_size)

    def slice_operations(self, vec_reg_size):
        count = self.opcount_filling_vector_register(vec_reg_size)
        assert count > 0
        newoplist = self.operations[count:]
        oplist = self.operations[:count]
        assert len(newoplist) + len(oplist) == len(self.operations)
        assert len(newoplist) != 0
        return oplist, newoplist

    def rightmost_match_leftmost(self, other):
        """ Check if pack A can be combined with pack B """
        assert isinstance(other, Pack)
        rightmost = self.operations[-1]
        leftmost = other.operations[0]
        # if it is not accumulating it is valid
        if self.is_accumulating():
            if not other.is_accumulating():
                return False
            elif self.position != other.position:
                return False
        return rightmost is leftmost

    def argument_vectors(self, state, pack, index, pack_args_index):
        vectors = []
        last = None
        for arg in pack_args_index:
            pos, vecop = state.getvector_of_box(arg)
            if vecop is not last and vecop is not None:
                vectors.append((pos, vecop))
                last = vecop
        return vectors

    def __repr__(self):
        if len(self.operations) == 0:
            return "Pack(empty)"
        packs = self.operations[0].op.getopname() + '[' + ','.join(['%2d' % (o.opidx) for o in self.operations]) + ']'
        if self.operations[0].op.getdescr():
            packs += 'descr=' + str(self.operations[0].op.getdescr())
        return "Pack(%dx %s)" % (self.numops(), packs)

    def is_accumulating(self):
        return False

    def clone(self, oplist):
        return Pack(oplist)

class Pair(Pack):
    """ A special Pack object with only two statements. """
    def __init__(self, left, right):
        assert isinstance(left, Node)
        assert isinstance(right, Node)
        Pack.__init__(self, [left, right])

    def __eq__(self, other):
        if isinstance(other, Pair):
            return self.left is other.left and \
                   self.right is other.right

class AccumPack(Pack):
    SUPPORTED = { rop.FLOAT_ADD: '+',
                  rop.INT_ADD:   '+',
                  rop.FLOAT_MUL: '*',
                }

    def __init__(self, nodes, operator, position):
        Pack.__init__(self, nodes)
        self.operator = operator
        self.position = position

    def getdatatype(self):
        accum = self.leftmost().getarg(self.position)
        vecinfo = forwarded_vecinfo(accum)
        return vecinfo.datatype

    def getbytesize(self):
        accum = self.leftmost().getarg(self.position)
        vecinfo = forwarded_vecinfo(accum)
        return vecinfo.bytesize

    def getleftmostseed(self):
        return self.leftmost().getarg(self.position)

    def getseeds(self):
        """ The accumulatoriable holding the seed value """
        return [op.getoperation().getarg(self.position) for op in self.operations]

    def reduce_init(self):
        if self.operator == '*':
            return 1
        return 0

    def is_accumulating(self):
        return True

    def clone(self, oplist):
        return AccumPack(oplist, self.operator, self.position)


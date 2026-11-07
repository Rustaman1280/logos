"""Threads, mutexes, channels and atomics: the Task 12 surface.

Three layers, one class for the checker, one for the lowerer, one for real
binaries, plus the C runtime on its own.

The checker's share is the shape of the vocabulary: the constructors and their
handle types, the `(shared T)` marker that makes shared mutable state visible,
and the argument rules a thread inherits from the language (a value that owns
memory cannot be copied into a new thread, and a capturing closure cannot leave
its frame). The lowerer's share is the ABI: one runtime call per operation and
one generated thunk per spawn site, which unpacks the argument record and calls
the target. The exec share is a real binary at ``-O0`` and ``-O2`` whose output
is deterministic because every thread is joined before the program prints.

The runtime's share is the leak tracker under contention. It was a hash map of
plain globals; a thread that allocates while another frees is a data race in the
debug build every test uses, so the probes here run the tracker from eight
threads at once and check both directions: no leak when every block is released,
and one correctly sized, correctly pathed line for every block that is not.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

from logos import driver, ir, llvm, lower
from logos.check import CheckedModule, check_module
from logos.diag import LogosError
from logos.parser import parse
from logos.resolve import resolve
from logos.types import Atomic, Chan, Prim, Ptr, Shared, StructRef

ROOT = Path(__file__).resolve().parents[1]
THREADS = ROOT / "tests" / "thread"
SUM = THREADS / "sum.logos"
CHAN = THREADS / "chan.logos"
BUILD = ROOT / "build"


def checked(source: str) -> CheckedModule:
    """Parse and check one inline module, which imports nothing."""

    def loader(name: str):
        raise FileNotFoundError(name)

    return check_module(resolve(parse(source, "m.logos"), loader))


def lowered(source: str) -> ir.IrModule:
    return lower.lower_module(checked(source))


def checked_importing(sources: dict[str, str], source: str) -> CheckedModule:
    """Check ``source`` with ``sources`` answering its ``(use ...)`` forms."""

    def loader(name: str):
        if name not in sources:
            raise FileNotFoundError(name)
        return parse(sources[name], f"{name}.logos")

    return check_module(resolve(parse(source, "m.logos"), loader))


def emitted(source: str) -> str:
    return llvm.emit(lowered(source))


def path_of(cm: CheckedModule, kind: str) -> str:
    for node in cm.module.root.walk():
        if node.kind == kind:
            return node.path
    raise AssertionError(f"no '{kind}' node in the module")


def type_of(cm: CheckedModule, kind: str):
    return cm.expr_types[path_of(cm, kind)]


def fail_on(source: str) -> LogosError:
    try:
        checked(source)
    except LogosError as error:
        return error
    raise AssertionError(f"expected a diagnostic, but this checked: {source}")


def fail_on_lower(source: str) -> LogosError:
    try:
        lowered(source)
    except LogosError as error:
        return error
    raise AssertionError(f"expected a diagnostic, but this lowered: {source}")


def calls(fn: ir.IrFn) -> list[tuple]:
    return [ins for block in fn.blocks for ins in block.instrs if ins[0] == "call"]


def callees(fn: ir.IrFn) -> list[str]:
    return [ins[2] for ins in calls(fn)]


def functions(module: ir.IrModule, suffix: str) -> list[ir.IrFn]:
    return [fn for fn in module.functions if fn.symbol and suffix in fn.symbol]


class ThreadTypeTest(unittest.TestCase):
    """The vocabulary: handle types, the shared marker and the argument rules."""

    def test_spawn_returns_a_thread(self):
        cm = checked(
            "(mod m)(fn w :void () (ret))"
            "(fn f :void () (let _ (spawn w)))"
        )
        self.assertEqual(type_of(cm, "spawn"), Prim("thread"))

    def test_join_takes_a_thread_and_returns_void(self):
        cm = checked(
            "(mod m)(fn w :void () (ret))"
            "(fn f :void () (let t (spawn w)) (join t))"
        )
        self.assertEqual(type_of(cm, "join"), Prim("void"))

    def test_join_needs_a_thread(self):
        error = fail_on("(mod m)(fn f :void ((n :i64)) (join n))")
        self.assertEqual(error.code, "E0100")
        self.assertEqual(error.message, "join needs a :thread, found :i64")

    def test_a_mutex_is_shared_and_unmarked_mutex_is_e0700(self):
        cm = checked("(mod m)(fn f :void () (let _ (mutex)))")
        self.assertEqual(
            cm.expr_types[path_of(cm, "mutex")], Shared(Prim("mutex"))
        )
        error = fail_on(
            "(mod m)(fn f :void ((m :mutex)) (lock m))"
        )
        self.assertEqual(error.code, "E0700")
        self.assertIn("(shared ...)", error.message)

    def test_a_thread_handle_is_shared_state_for_e0700(self):
        # S4: join releases the handle, so two threads holding one address can
        # join it twice. Handing a thread to spawn is a crossing like any
        # other, and it must say (shared :thread) out loud.
        error = fail_on(
            "(mod m)(fn w :void ((t :thread)) (join t))"
            "(fn noop :void () (ret))"
            "(fn f :void () (let t (spawn noop)) (spawn w t))"
        )
        self.assertEqual(error.code, "E0700")
        self.assertIn("(shared ...)", error.message)

    def test_a_marked_thread_handle_is_spellable(self):
        # The marker is what makes the crossing visible; there is no unwrap
        # yet, so holding one is all a program can do with it.
        checked(
            "(mod m)(fn hold :void ((t (shared :thread))) (let _ t))"
        )

    def test_a_channel_of_an_empty_type_is_refused(self):
        # S5: the runtime copies one byte per element while the caller passes
        # a zero-byte slot, which is an out-of-bounds stack write.
        error = fail_on(
            "(mod m)(fn f :void () (letmut c (chan (arr :i64 0) 4)))"
        )
        self.assertEqual(error.code, "E0002")
        self.assertEqual(
            error.message, "a channel of an empty type is not supported"
        )

    def test_a_channel_of_an_empty_struct_is_refused(self):
        error = fail_on(
            "(mod m)(type nothing (struct))"
            "(fn f :void () (letmut c (chan nothing 1)))"
        )
        self.assertEqual(error.code, "E0002")
        self.assertEqual(
            error.message, "a channel of an empty type is not supported"
        )

    def test_a_channel_of_a_non_empty_struct_is_still_allowed(self):
        checked(
            "(mod m)(type cell (struct (v :i64)))"
            "(fn f :void () (letmut _ (chan cell 1)))"
        )

    def test_lock_and_unlock_need_a_shared_mutex(self):
        cm = checked(
            "(mod m)(fn f :void ((m (shared :mutex))) (lock m) (unlock m))"
        )
        self.assertEqual(type_of(cm, "lock"), Prim("void"))
        self.assertEqual(type_of(cm, "unlock"), Prim("void"))
        error = fail_on('(mod m)(fn f :void ((m :i64)) (lock m))')
        self.assertEqual(error.code, "E0100")

    def test_a_channel_carries_its_element_type(self):
        cm = checked("(mod m)(fn f :void () (let _ (chan :i64 4)))")
        self.assertEqual(
            cm.expr_types[path_of(cm, "chan")], Shared(Chan(Prim("i64")))
        )

    def test_send_takes_the_element_and_recv_returns_it(self):
        cm = checked(
            "(mod m)(fn f :void () (let c (chan :i64 4)) (send c 7)"
            " (let n (recv c)) (println n))"
        )
        self.assertEqual(type_of(cm, "send"), Prim("void"))
        self.assertEqual(type_of(cm, "recv"), Prim("i64"))
        error = fail_on(
            '(mod m)(fn f :void () (let c (chan :i64 1)) (send c "x"))'
        )
        self.assertEqual(error.code, "E0100")

    def test_a_channel_needs_a_capacity(self):
        for source in (
            "(mod m)(fn f :void () (let c (chan :i64)))",
            '(mod m)(fn f :void () (let c (chan :i64 "four")))',
        ):
            with self.subTest(source=source):
                error = fail_on(source)
                self.assertIn(error.code, ("E0102", "E0100"))

    def test_a_channel_of_bare_pointers_is_e0700(self):
        # The ring is a thread crossing like any other: an element that is a
        # raw address has to say `(shared ...)`, at the element type.
        error = fail_on("(mod m)(fn f :void () (let c (chan (ptr :i64) 1)))")
        self.assertEqual(error.code, "E0700")
        self.assertEqual(error.path, "m/f/2/1/0")
        self.assertIn("(shared ...)", error.message)

    def test_a_channel_of_structs_holding_a_pointer_is_e0700(self):
        error = fail_on(
            "(mod m)(type holder (struct (p (ptr :i64))))"
            "(fn f :void () (let c (chan holder 1)))"
        )
        self.assertEqual(error.code, "E0700")
        self.assertEqual(error.path, "m/f/2/1")

    def test_a_spelled_channel_of_bare_pointers_is_e0700(self):
        # A signature names the element type too, and the marker rule does not
        # wait for a `(chan ...)` constructor to apply.
        error = fail_on(
            "(mod m)(fn f :void ((c (shared (chan (ptr :i64))))) (ret))"
        )
        self.assertEqual(error.code, "E0700")

    def test_a_channel_of_marked_pointers_checks(self):
        cm = checked("(mod m)(fn f :void () (let _ (chan (shared (ptr :i64)) 1)))")
        self.assertEqual(
            cm.expr_types[path_of(cm, "chan")],
            Shared(Chan(Shared(Ptr(Prim("i64"))))),
        )

    def test_a_channel_of_structs_holding_a_marked_pointer_checks(self):
        # The marker is a stop at any depth: the field is already accounted
        # for, so the element crosses and both ends see the pointee.
        cm = checked(
            "(mod m)(type holder (struct (p (shared (ptr :i64)))))"
            "(fn f :void ((p (shared (ptr :i64))))"
            " (let c (chan holder 1))"
            " (send c (make holder p))"
            " (let got (recv c))"
            " (println (deref (get got p))))"
        )
        self.assertEqual(type_of(cm, "recv"), StructRef("holder"))

    def test_an_atomic_is_shared_and_typed(self):
        cm = checked(
            "(mod m)(fn f :void () (let a (atomic :i64 0))"
            " (atomic-add a 1) (atomic-store a 2) (let n (atomic-load a))"
            " (println n))"
        )
        self.assertEqual(
            cm.expr_types[path_of(cm, "atomic")], Shared(Atomic(Prim("i64")))
        )
        self.assertEqual(type_of(cm, "atomic-add"), Prim("i64"))
        self.assertEqual(type_of(cm, "atomic-load"), Prim("i64"))

    def test_an_atomic_needs_a_sixty_four_bit_integer(self):
        error = fail_on("(mod m)(fn f :void () (let a (atomic :i32 0)))")
        self.assertEqual(error.code, "E0100")
        self.assertIn(":i32", error.message)

    def test_a_shared_pointer_is_a_spellable_type(self):
        cm = checked(
            "(mod m)(fn f :void () (let p (alloc :i64 1))"
            " (letmut s (shared (ptr :i64)) (ptr p))"
            " (set-deref s 3) (println (deref s)) (free p))"
        )
        self.assertEqual(cm.expr_types[path_of(cm, "deref")], Prim("i64"))

    def test_only_a_pointer_or_a_handle_can_be_shared(self):
        error = fail_on("(mod m)(fn f :void ((n (shared :i64))) (println n))")
        self.assertEqual(error.code, "E0002")
        self.assertIn("shared", error.message)

    def test_a_bare_pointer_argument_to_spawn_is_e0700(self):
        error = fail_on(
            "(mod m)(fn w :void ((p (ptr :i64))) (ret))"
            "(fn f :void ((o :(own :i64))) (spawn w (ptr o)))"
        )
        self.assertEqual(error.code, "E0700")
        self.assertEqual(error.path, "m/f/2/1")

    def test_a_marked_pointer_argument_to_spawn_checks(self):
        cm = checked(
            "(mod m)(fn w :void ((p (shared (ptr :i64)))) (ret))"
            "(fn f :void ((p (shared (ptr :i64)))) (let t (spawn w p)) (join t))"
        )
        self.assertEqual(type_of(cm, "spawn"), Prim("thread"))

    def test_a_pointer_field_in_a_struct_handed_to_spawn_is_e0700(self):
        # The copy carries the address across the boundary even though the
        # argument's spelling — `holder` — never says `(ptr ...)`.
        error = fail_on(
            "(mod m)(type holder (struct (p (ptr :i64))))"
            "(fn w :void ((h holder)) (ret))"
            "(fn f :void ((h holder)) (spawn w h))"
        )
        self.assertEqual(error.code, "E0700")
        self.assertEqual(error.path, "m/f/2")

    def test_an_array_of_pointers_in_a_struct_handed_to_spawn_is_e0700(self):
        error = fail_on(
            "(mod m)(type holder (struct (a (arr (ptr :i64) 2))))"
            "(fn w :void ((h holder)) (ret))"
            "(fn f :void ((h holder)) (spawn w h))"
        )
        self.assertEqual(error.code, "E0700")
        self.assertEqual(error.path, "m/f/2")

    def test_a_pointer_deep_in_nested_aggregates_is_e0700(self):
        error = fail_on(
            "(mod m)(type inner (struct (p (ptr :i64))))"
            "(type outer (struct (i inner)))"
            "(fn w :void ((o outer)) (ret))"
            "(fn f :void ((o outer)) (spawn w o))"
        )
        self.assertEqual(error.code, "E0700")

    def test_a_marked_pointer_in_an_aggregate_field_is_still_legal(self):
        # The marker is a stop, wherever it is written: the field is already
        # accounted for, so the struct may cross a thread boundary.
        for field in ("(p (shared (ptr :i64)))",
                      "(a (arr (shared (ptr :i64)) 2))"):
            with self.subTest(field=field):
                cm = checked(
                    f"(mod m)(type holder (struct {field}))"
                    "(fn w :void ((h holder)) (ret))"
                    "(fn f :void ((h holder)) (let t (spawn w h)) (join t))"
                )
                self.assertEqual(type_of(cm, "spawn"), Prim("thread"))

    def test_a_thread_cannot_be_started_with_a_vector(self):
        error = fail_on(
            "(mod m)(fn w :void ((v (vec :i64))) (vec-free v))"
            "(fn f :void () (letmut v (vec :i64) (make (vec :i64)))"
            " (spawn w v))"
        )
        self.assertEqual(error.code, "E0402")
        self.assertIn("copied", error.message)

    def test_a_thread_cannot_be_started_with_an_owner(self):
        error = fail_on(
            "(mod m)(fn w :void ((p (own :i64))) (free p))"
            "(fn f :void () (let p (alloc :i64 1)) (spawn w p))"
        )
        self.assertEqual(error.code, "E0402")

    def test_a_thread_target_must_return_void(self):
        error = fail_on(
            "(mod m)(fn w :i64 () (ret 1))"
            "(fn f :void () (let t (spawn w)) (join t))"
        )
        self.assertEqual(error.code, "E0100")

    def test_a_thread_target_must_be_a_named_module_function(self):
        error = fail_on(
            "(mod m)(fn f :void ((g :(fn () :void))) (let t (spawn g)) (join t))"
        )
        self.assertEqual(error.code, "E0103")

    def test_a_generic_target_is_refused_because_nothing_fixes_its_types(self):
        error = fail_on(
            "(mod m)(fn w :void ((x :T)) (ret))"
            "(fn f :void ((n :i64)) (let t (spawn w n)) (join t))"
        )
        self.assertEqual(error.code, "E0501")

    def test_a_test_local_helper_cannot_be_spawned(self):
        # A helper has no lowered definition, so a thunk calling it could not
        # link. It is refused at its own node now, before the spawn is seen.
        error = fail_on(
            "(mod m)(test t (expect (code 0))"
            " (fn helper :void () (ret))"
            " (spawn helper))"
        )
        self.assertEqual(error.code, "E0002")
        self.assertEqual(
            error.message, "test-local helper functions are not lowered yet"
        )

    def test_an_imported_function_cannot_be_spawned_and_says_why(self):
        # A different reason with the same answer: the definition exists, but
        # in another module, so this module has no symbol to point a thunk at.
        try:
            checked_importing(
                {"lib": "(mod lib)(fn work :void () (ret))"},
                "(mod m)(use lib)(fn f :void () (spawn work))",
            )
        except LogosError as error:
            self.assertEqual(error.code, "E0103")
            self.assertIn("imported", error.message)
            self.assertIn("not lowered yet", error.message)
        else:
            self.fail("spawning an imported function was accepted")

    def test_a_channel_cannot_carry_a_capturing_closure(self):
        error = fail_on(
            "(mod m)(fn f :void ((c (shared (chan (fn () :void)))) (x :i64))"
            " (send c (fn :void () (println x))))"
        )
        self.assertEqual(error.code, "E0502")

    def test_a_program_that_starts_a_thread_is_still_checked(self):
        # No special form leaks a result: a spawn in statement position is a
        # `:thread`, and dropping one is not an unhandled result.
        cm = checked(
            "(mod m)(fn w :void () (ret))"
            "(fn f :void () (spawn w))"
        )
        self.assertEqual(type_of(cm, "spawn"), Prim("thread"))


class ThreadLoweringTest(unittest.TestCase):
    """The ABI: one runtime call per operation, one thunk per spawn site."""

    def test_spawn_allocates_an_argument_record_and_starts_a_thread(self):
        module = lowered(
            "(mod m)(fn w :void ((n :i64)) (println n))"
            "(fn f :void () (let t (spawn w 7)) (join t))"
        )
        f = next(fn for fn in module.functions if fn.symbol == "lg.m.f")
        self.assertIn("rt_alloc", callees(f))
        self.assertIn("rt_thread_new", callees(f))
        self.assertIn("rt_join", callees(f))
        thunks = functions(module, "$spawn")
        self.assertEqual(len(thunks), 1)
        # The thunk unpacks the record and calls the target by symbol.
        self.assertIn("lg.m.w", callees(thunks[0]))

    def test_spawn_names_the_spawn_node_in_the_argument_record(self):
        text = emitted(
            "(mod m)(fn w :void ((n :i64)) (println n))"
            "(fn f :void () (let _ (spawn w 7)))"
        )
        self.assertIn("@rt_thread_new", text)
        # The path of the spawn is the alloc's path, so an unjoined thread's
        # argument record would be reported at the form that started it.
        self.assertIn("m/f/2", text)

    def test_join_passes_the_handle_and_the_join_path(self):
        text = emitted(
            "(mod m)(fn w :void () (ret))"
            "(fn f :void () (let t (spawn w)) (join t))"
        )
        self.assertIn("declare void @rt_join(i8*, i8*)", text)

    def test_mutex_operations_call_the_runtime(self):
        module = lowered(
            "(mod m)(fn f :void ((m (shared :mutex))) (lock m) (unlock m))"
        )
        self.assertEqual(
            [name for name in callees(module.functions[0])
             if name.startswith("rt_mutex")],
            ["rt_mutex_lock", "rt_mutex_unlock"],
        )

    def test_a_mutex_constructor_calls_rt_mutex_new(self):
        module = lowered("(mod m)(fn f :void () (let _ (mutex)))")
        self.assertIn("rt_mutex_new", callees(module.functions[0]))

    def test_channel_operations_pass_the_element_size(self):
        text = emitted(
            "(mod m)(fn f :void () (let c (chan :i64 4)) (send c 7)"
            " (let _ (recv c)))"
        )
        self.assertIn("declare i8* @rt_chan_new(i64, i64, i8*)", text)
        self.assertIn("@rt_chan_send", text)
        self.assertIn("@rt_chan_recv", text)
        # One i64 element: eight bytes, passed as the first argument.
        self.assertIn("call i8* @rt_chan_new(i64 8, i64 4,", text)

    def test_atomic_operations_call_the_runtime(self):
        module = lowered(
            "(mod m)(fn f :void () (let a (atomic :i64 0))"
            " (atomic-add a 1) (atomic-store a 2) (atomic-load a))"
        )
        self.assertEqual(
            [name for name in callees(module.functions[0]) if name.startswith("rt_atomic")],
            ["rt_atomic_new", "rt_atomic_add", "rt_atomic_store", "rt_atomic_load"],
        )

    def test_a_channel_of_structs_copies_the_whole_element(self):
        # The element size is the struct's, not the handle's: a ring that
        # copied eight bytes of a sixteen-byte value would deliver a correct
        # first field and whatever the stack held for the second.
        text = emitted(
            "(mod m)(type pair (struct (a :i64) (b :i64)))"
            "(fn f :void () (let _ (chan pair 4)))"
        )
        self.assertIn("call i8* @rt_chan_new(i64 16, i64 4,", text)

    def test_send_evaluates_its_channel_before_its_element(self):
        # Operands left to right, like every other form: a send whose channel
        # is a call blocks on that call before it evaluates the element, so
        # `(send (recv a) (/ 1 0))` waits on the channel rather than panicking.
        module = lowered(
            "(mod m)(fn ch :(shared (chan :i64)) ()"
            " (letmut c (chan :i64 2)) (ret c))"
            "(fn n :i64 () (ret 1))"
            "(fn f :void () (send (ch) (n)))"
        )
        f = next(fn for fn in module.functions if fn.symbol == "lg.m.f")
        order = callees(f)
        self.assertIn("lg.m.ch", order)
        self.assertIn("lg.m.n", order)
        self.assertLess(order.index("lg.m.ch"), order.index("lg.m.n"))
        self.assertEqual(order[-1], "rt_chan_send")

    def test_a_shared_pointer_deref_uses_the_pointee_type(self):
        text = emitted(
            "(mod m)(fn f :void ((p (shared (ptr :i64))))"
            " (println (deref p)))"
        )
        self.assertIn("load i64, i64*", text)


class ThreadExecTest(unittest.TestCase):
    """Real binaries, eager the same answer at ``-O0`` and ``-O2``."""

    def run_case(self, source: Path, name: str, opt: str) -> subprocess.CompletedProcess:
        target = str(BUILD / f"thread-{source.stem}-{name}-{opt[1:]}")
        driver.run_test(str(source), name, target, opt=opt)
        return subprocess.run([target], capture_output=True, text=True)

    def test_the_atomic_sum(self):
        for opt in ("-O0", "-O2"):
            with self.subTest(opt=opt):
                done = self.run_case(SUM, "eight-threads", opt)
                self.assertEqual(done.stdout, "800000\n", done.stderr)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertNotIn("LEAK", done.stderr)

    def test_the_sum_is_stable_over_a_thousand_runs(self):
        # The build is Task 12's stress run: one binary, a thousand starts, so
        # a lost increment or a start that never joins shows up as anything but
        # 800000. Building a thousand times would test clang, not the program.
        target = str(BUILD / "thread-sum-stress")
        driver.run_test(str(SUM), "eight-threads", target, opt="-O2")
        for run in range(1000):
            done = subprocess.run([target], capture_output=True, text=True)
            self.assertEqual(done.stdout, "800000\n", f"run {run}: {done.stderr}")
            self.assertEqual(done.returncode, 0, f"run {run}: {done.stderr}")

    def test_the_channel_pipeline(self):
        for opt in ("-O0", "-O2"):
            with self.subTest(opt=opt):
                done = self.run_case(CHAN, "pipeline", opt)
                self.assertEqual(done.stdout, "500500\n", done.stderr)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertNotIn("LEAK", done.stderr)

    def test_the_mutex_protects_a_shared_cell(self):
        for opt in ("-O0", "-O2"):
            with self.subTest(opt=opt):
                done = self.run_case(CHAN, "mutex-counter", opt)
                self.assertEqual(done.stdout, "40000\n", done.stderr)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertNotIn("LEAK", done.stderr)

    def test_a_channel_of_structs(self):
        for opt in ("-O0", "-O2"):
            with self.subTest(opt=opt):
                done = self.run_case(CHAN, "struct-elements", opt)
                self.assertEqual(done.stdout, "90\n", done.stderr)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertNotIn("LEAK", done.stderr)

    def test_a_channel_of_shared_pointer_structs(self):
        for opt in ("-O0", "-O2"):
            with self.subTest(opt=opt):
                done = self.run_case(CHAN, "shared-pointer-element", opt)
                self.assertEqual(done.stdout, "7\n", done.stderr)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertNotIn("LEAK", done.stderr)

    def test_four_producers_and_one_consumer(self):
        for opt in ("-O0", "-O2"):
            with self.subTest(opt=opt):
                done = self.run_case(CHAN, "four-producers", opt)
                self.assertEqual(done.stdout, "49995000\n", done.stderr)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertNotIn("LEAK", done.stderr)

    def test_a_channel_of_zero_capacity_is_a_runtime_check(self):
        done = self.run_case(CHAN, "zero-capacity", "-O0")
        self.assertEqual(done.returncode, 2)
        self.assertIn("R0003: channel capacity must be positive", done.stderr)

    def test_joining_twice_is_r0005(self):
        done = self.run_case(CHAN, "double-join", "-O0")
        self.assertEqual(done.returncode, 2)
        self.assertIn("R0005: thread is not running", done.stderr)


TRACKER_PROBE = r"""
#include <pthread.h>
#include "rt.h"

#define THREADS 8
#define BLOCKS 1000

static void *churn(void *arg) {
    long long i;
    (void)arg;
    for (i = 0; i < BLOCKS; i++) {
        void *p = rt_alloc(16, 8, "probe/churn");
        rt_free(p);
    }
    return NULL;
}

int main(void) {
    pthread_t threads[THREADS];
    int i;
    for (i = 0; i < THREADS; i++) {
        if (pthread_create(&threads[i], NULL, churn, NULL) != 0) return 9;
    }
    for (i = 0; i < THREADS; i++) {
        if (pthread_join(threads[i], NULL) != 0) return 9;
    }
    rt_print_i64((long long)rt_leak_count());
    rt_println();
    rt_leak_report();
    return rt_leak_count() == 0 ? 0 : 1;
}
"""

LEAK_PROBE = r"""
#include <pthread.h>
#include "rt.h"

#define THREADS 8

typedef struct { int index; } job;

static void *leak(void *raw) {
    job *j = raw;
    size_t size = (size_t)(j->index + 1) * 8;
    (void)rt_alloc(size, 8, "probe/leak");
    return NULL;
}

int main(void) {
    pthread_t threads[THREADS];
    job jobs[THREADS];
    int i;
    for (i = 0; i < THREADS; i++) {
        jobs[i].index = i;
        if (pthread_create(&threads[i], NULL, leak, &jobs[i]) != 0) return 9;
    }
    for (i = 0; i < THREADS; i++) {
        if (pthread_join(threads[i], NULL) != 0) return 9;
    }
    rt_leak_report();
    return rt_leak_count() == THREADS ? 0 : 1;
}
"""

PRIMITIVE_PROBE = r"""
#include <pthread.h>
#include "rt.h"

#define COUNT 1000

static void *sender(void *raw) {
    rt_chan *c = raw;
    long long i;
    for (i = 1; i <= COUNT; i++) {
        rt_chan_send(c, &i, "probe/chan");
    }
    return NULL;
}

int main(void) {
    rt_atomic *a = rt_atomic_new(0);
    rt_chan *c = rt_chan_new(sizeof(long long), 4, "probe/chan");
    pthread_t t;
    long long sum = 0;
    long long i;

    if (pthread_create(&t, NULL, sender, c) != 0) return 9;
    for (i = 0; i < COUNT; i++) {
        long long v = 0;
        rt_chan_recv(c, &v, "probe/chan");
        sum += v;
    }
    if (pthread_join(t, NULL) != 0) return 9;

    for (i = 0; i < 4; i++) rt_atomic_add(a, 10);
    rt_atomic_store(a, rt_atomic_load(a) + 1);
    rt_print_i64(sum);
    rt_println();
    rt_print_i64(rt_atomic_load(a));
    rt_println();
    return (sum == 500500 && rt_atomic_load(a) == 41) ? 0 : 1;
}
"""


def build_probe(name: str, source: str) -> str:
    """Compile one C probe against the debug runtime and return its path."""
    path = BUILD / f"{name}.c"
    binary = BUILD / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    done = subprocess.run(
        [
            "clang",
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-DLOGOS_DEBUG=1",
            "-I",
            str(ROOT / "runtime"),
            str(path),
            str(ROOT / "runtime" / "rt.c"),
            "-o",
            str(binary),
            "-lpthread",
            "-lm",
        ],
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        raise AssertionError(f"{name} did not build:\n{done.stderr}")
    return str(binary)


class RuntimeThreadProbeTest(unittest.TestCase):
    """The C runtime on its own: tracking, channels, atomics, mutexes."""

    @classmethod
    def setUpClass(cls):
        cls.tracker = build_probe("thread-probe-tracker", TRACKER_PROBE)
        cls.leak = build_probe("thread-probe-leak", LEAK_PROBE)
        cls.primitive = build_probe("thread-probe-primitive", PRIMITIVE_PROBE)
        cls.null_atomic = build_probe("thread-probe-atomic-null", r"""
#include "rt.h"

int main(void) {
    (void)rt_atomic_load(NULL);
    return 0;
}
""")

    def test_concurrent_alloc_and_free_leaves_no_leak(self):
        done = subprocess.run([self.tracker], capture_output=True, text=True)
        self.assertEqual(done.stdout, "0\n", done.stderr)
        self.assertEqual(done.stderr, "")
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_every_leak_from_every_thread_is_reported(self):
        done = subprocess.run([self.leak], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        lines = sorted(line for line in done.stderr.splitlines() if line)
        expected = sorted(
            f"LEAK {(i + 1) * 8} bytes at probe/leak" for i in range(8)
        )
        self.assertEqual(lines, expected)

    def test_channels_atomics_and_mutexes_agree_under_threads(self):
        done = subprocess.run([self.primitive], capture_output=True, text=True)
        self.assertEqual(done.stdout, "500500\n41\n", done.stderr)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_null_atomic_handle_panics_r0005(self):
        # Every other concurrency failure is R0005, `thread operation failed`;
        # a null handle is one, not a runtime range check.
        done = subprocess.run([self.null_atomic], capture_output=True, text=True)
        self.assertEqual(done.returncode, 2)
        self.assertIn("R0005: atomic handle is null", done.stderr)


class RuntimeMutexProbeTest(unittest.TestCase):
    """The mutex is error-checking: a double lock fails instead of hanging."""

    @classmethod
    def setUpClass(cls):
        cls.binary = build_probe("thread-probe-mutex", r"""
#include "rt.h"

int main(void) {
    rt_mutex *m = rt_mutex_new("probe/mutex");
    rt_mutex_lock(m, "probe/lock");
    rt_mutex_lock(m, "probe/relock");
    return 0;
}
""")

    def test_a_recursive_lock_panics_r0005(self):
        done = subprocess.run([self.binary], capture_output=True, text=True)
        self.assertEqual(done.returncode, 2)
        self.assertIn("probe/relock: R0005:", done.stderr)

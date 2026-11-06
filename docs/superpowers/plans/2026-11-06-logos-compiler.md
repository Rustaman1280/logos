# Logos Compiler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build Logos, a prefix-form language for AI readers, and `logosc`, its compiler to native x86_64 Linux binaries.

**Architecture:** A Python front end (lexer, parser, resolver, type checker) lowers a typed AST to a small flat IR, then prints LLVM IR text. clang compiles that text and links it against a C runtime. Tests are grammar nodes; a harness modelled on GCC runs them.

**Tech Stack:** Python 3.14 (standard library only), clang 14 from apt, LLVM IR text, C runtime, GNU make.

**Spec:** `docs/superpowers/specs/2026-11-06-ai-language-design.md`

## Global Constraints

- Host: Ubuntu 22.04, x86_64. Target triple: `x86_64-unknown-linux-gnu`.
- `clang` and `lld` installed with `apt-get install -y clang lld`.
- Python: standard library only. No packages beyond `pytest`-free `unittest`.
- Integer arithmetic wraps; division by zero panics with a node path. No undefined behaviour anywhere.
- `:str` is `{ptr :u8, len :u64}`, UTF-8, length in bytes, never NUL-terminated.
- Release is manual: `free`, no compiler-inserted drops.
- Type names have no aliases. `:int` does not exist; source writes `:i64`.
- Every diagnostic carries a location (a node path, else file:line:column) and a code like `E0421`.
- Runtime is C (`runtime/rt.c`), reached from Logos only through `extern`.
- Tests are `(test ...)` nodes in normal grammar; `build` compiles them out.

## Review Focus

1. A multi-line string prints in full, byte for byte, with its length. Wrong: `%s` on a fat pointer truncates silently.
2. Division by zero panics with a path and exit code 2, not a SIGFPE.
3. A double free panics; it never corrupts the heap silently.
4. Anything that works at `-O0` works at `-O2` with identical output.
5. A thread test is deterministic: joined before exit, no interleaved output.

---

### Task 1: Toolchain and repository scaffold

**Files:**
- Create: `pyproject.toml`, `logos/__init__.py`, `logos/cli.py`, `logos/__main__.py`, `Makefile`, `README.md`, `.gitignore`, `tests/.gitkeep`
- Create: `runtime/rt.h`, `runtime/rt.c`

**Interfaces:**
- Produces: `logos.cli.main(argv: list[str]) -> int` with subcommands `build`, `test`, `fmt`, `show`, `patch`, `version`.
- Produces: `python3 -m logos version` prints `logos 0.1.0`.

- [ ] **Step 1: Install the toolchain**

Run: `apt-get update && apt-get install -y clang lld`
Expected: `clang --version` prints `Ubuntu clang version 14.0.x`.

- [ ] **Step 2: Write `tests/test_cli.py`**

```python
from logos.cli import main

def test_version(capsys):
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == "logos 0.1.0"
```

- [ ] **Step 3: Run it to confirm it fails**

Run: `python3 -m unittest tests.test_cli -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'logos.cli'`.

- [ ] **Step 4: Implement `logos/cli.py` with an argparse skeleton**

`main` parses the subcommand, returns 0 on `version`, otherwise returns 0 and prints `not implemented`. `Makefile` gets `test`, `fmt` and `clean` targets, each calling `python3 -m unittest` or `python3 -m logos`.

- [ ] **Step 5: Run the test, then commit**

```bash
python3 -m unittest tests.test_cli -v && git add -A && git commit -m "chore: scaffold logos repo, cli and runtime placeholders"
```

---

### Task 2: Lexer, parser, node paths and canonical printer

**Files:**
- Create: `logos/lexer.py`, `logos/parser.py`, `logos/nodes.py`, `logos/fmt.py`
- Test: `tests/test_parser.py`, `tests/test_fmt.py`

**Interfaces:**
- Produces: `logos.lexer.tokenize(src: str, file: str) -> list[Token]`, `Token(kind: str, value: str, line: int, col: int)`, kinds `LPAREN RPAREN ATOM STRING NUMBER`.
- Produces: `logos.parser.parse(src: str, file: str) -> Node`.
- Produces: `logos.nodes.Node(kind: str, items: tuple, path: str, line: int, col: int)`; children are `Node` or `Token`; `Node.walk()` yields every node in order.
- Produces: `logos.fmt.render(node: Node) -> str`, two-space indent, one child per line when the node has more than one, trailing newline.

**Path rule:** a named definition inserts its name (`mod math` -> `math`, `fn add` -> `math/add`); every other node appends its position among the parent's items, numbered from zero after the head (and after the name). So `(ret (+ a b))` inside `add` is `math/add/2`, and `(+ a b)` is `math/add/2/0`. A file with `(mod math)` and two nodes has paths `math/0`, `math/1`. Test nodes insert `test/<name>`.

- [ ] **Step 1: Write the failing tests**

```python
from logos.parser import parse
from logos.fmt import render

SRC = '(mod math)\n(fn add :i64 ((a :i64) (b :i64))\n  (ret (+ a b)))\n'

def test_paths():
    root = parse(SRC, "math.logos")
    assert [n.path for n in root.walk() if n.kind in ("mod", "fn", "ret")] == [
        "math", "math/add", "math/add/2/0"]

def test_fmt_is_idempotent():
    once = render(parse(SRC, "math.logos"))
    assert render(parse(once, "math.logos")) == once

def test_unbalanced_paren_is_a_diagnostic():
    with pytest.raises(LogosError) as e:
        parse("(mod x", "x.logos")
    assert e.value.code == "E0001"
```

- [ ] **Step 2: Run to confirm failure**

Run: `python3 -m unittest tests.test_parser tests.test_fmt -v`
Expected: FAIL, import error.

- [ ] **Step 3: Implement lexer, parser, nodes and printer**

The parser builds nodes without knowing any grammar: `(` starts a node whose head token becomes `kind`. Grammar checking belongs to the resolver and the checker.

- [ ] **Step 4: Prove idempotency on a real file**

Run: `cp tests/parse/fixture.logos /tmp/a && python3 -m logos fmt /tmp/a && cp /tmp/a /tmp/b && python3 -m logos fmt /tmp/b && diff /tmp/a /tmp/b`
Expected: no output, exit 0, and the fixture itself stays canonical. `fmt FILE` rewrites in place. `tests/parse/fixture.logos` holds one module with a function, a struct and a test node.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: sexpr lexer, parser with node paths, canonical printer"
```

---

### Task 3: Diagnostics and name resolution

**Files:**
- Create: `logos/diag.py`, `logos/resolve.py`
- Test: `tests/test_resolve.py`, `tests/parse/` (first negative cases)

**Interfaces:**
- Produces: `logos.diag.LogosError(Exception)` with `code: str`, `path: str`, `message: str`, and `render() -> str` returning `"E0421 math/add/2/0: cannot add :i64 to :f64"`.
- Produces: `logos.diag.diag(code: str, path: str, message: str) -> LogosError`.
- Produces: `logos.resolve.resolve(root: Node, loader: Callable[[str], Node]) -> Module`, where `Module(name: str, root: Node, types: dict[str, Node], fns: dict[str, Node], tests: dict[str, Node])`.
- Produces: error codes `E0001` unbalanced parens, `E0002` unknown node, `E0003` duplicate definition, `E0004` circular import, `E0005` name not found.

- [ ] **Step 1: Write failing tests**

```python
def test_duplicate_fn_reports_the_second_path():
    with pytest.raises(LogosError) as e:
        resolve_src('(mod m)(fn f :i64 ()(ret 0))(fn f :i64 ()(ret 1))')
    assert e.value.code == "E0003"
    assert e.value.path == "m/f"

def test_circular_import_lists_the_cycle():
    with pytest.raises(LogosError) as e:
        resolve_src('(mod a)(use b)', files={"b": '(mod b)(use a)'})
    assert e.value.code == "E0004"
    assert "a -> b -> a" in e.value.message
```

- [ ] **Step 2: Run to confirm failure**, then implement `diag.py` and `resolve.py`.

- [ ] **Step 3: Add the negative corpus**

`tests/parse/unknown-node.logos` holding `(mod m)(wat 1)` plus a sibling `unknown-node.expect` containing `E0002`. The harness reads `.expect` in Task 13; until then a unit test reads it.

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "feat: diagnostics with paths, module resolution, import cycle check"
```

---

### Task 4: Types and the core checker

**Files:**
- Create: `logos/types.py`, `logos/check.py`
- Test: `tests/test_types.py`, `tests/type/`

**Interfaces:**
- Produces: `logos.types` with `Prim(name)`, `Ptr(elem)`, `Own(elem)`, `Arr(elem, n)`, `Vec(elem)`, `Res(ok, err)`, `Opt(elem)`, `FnTy(params: tuple, ret)`, `Shared(elem)`, `StructRef(name)`, `EnumRef(name)`, `TypeVar(name)`; all frozen dataclasses with `__str__` returning Logos syntax (`(vec :i64)`).
- Produces: `logos.types.parse_type(node: Node) -> Ty` and `logos.types.compatible(a: Ty, b: Ty) -> bool` (exact match after resolving type variables; no widening).
- Produces: `logos.check.check_module(m: Module) -> CheckedModule`; `CheckedModule.module: Module`, `types: dict[str, Ty]`, `fn_sigs: dict[str, FnTy]`, `expr_types: dict[str, Ty]` keyed by node path.
- Produces: error codes `E0100` type mismatch, `E0101` unknown type, `E0102` wrong arity, `E0103` unknown function.

- [ ] **Step 1: Write failing tests**

```python
def test_no_implicit_widening():
    err = expect_error('(mod m)(fn f :i64 ()(ret (+ (deref p) 1)))', "E0100")
    assert "i32" in err.message and "i64" in err.message

def test_signature_is_checked_before_body():
    expect_error('(mod m)(fn f :i64 ((a :i64))(ret 0))', None)  # passes
    expect_error('(mod m)(fn f :i64 ((a :wat))(ret 0))', "E0101")

def test_call_arity():
    expect_error('(mod m)(fn g :i64 ((a :i64))(ret a))(fn f :i64 ()(ret (g)))', "E0102")
```

- [ ] **Step 2: Run to confirm failure**, then implement `types.py`, then `check.py` for: primitives, arithmetic on equal types, comparisons, `let`/`letmut`, `set`, `if`, `while`, `ret`, function calls.

- [ ] **Step 3: Check every expression node records its type**

Run: `python3 -m unittest tests.test_types -v`
Expected: PASS, and a test asserts `expr_types["m/f/2/0"] == Prim("i64")` for `(+ a 1)` in `(fn f :i64 ((a :i64)) (ret (+ a 1)))`, under the positional path rule.

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "feat: type system and core checker with path-keyed results"
```

---

### Task 5: IR, LLVM emitter, first working binary

**Files:**
- Create: `logos/ir.py`, `logos/lower.py`, `logos/llvm.py`, `logos/driver.py`
- Create: `examples/hello.logos`
- Test: `tests/exec/hello.logos`, `tests/test_emit.py`

**Interfaces:**
- Produces: `logos.ir.IrModule(triple: str, globals: list, functions: list[IrFn])`, `IrFn(name: str, params: list[tuple[str, Ty]], ret: Ty, blocks: list[IrBlock])`, `IrBlock(label: str, instrs: list[tuple], term: tuple)`.
- Produces: `logos.lower.lower_module(cm: CheckedModule) -> IrModule`.
- Produces: `logos.llvm.emit(m: IrModule) -> str` (text, `target triple = "x86_64-unknown-linux-gnu"`).
- Produces: `logos.driver.build(src_path: str, out: str | None, opt: str = "-O2", keep_ir: bool = True) -> str` returning the binary path. Commands exactly: `clang {opt} -target x86_64-unknown-linux-gnu build/<name>.ll runtime/rt.c -o <out> -lpthread -lm`.
- Produces: `logos.driver.run_test(src_path: str, test_name: str, out: str) -> str` lowering one test body into `main`.

- [ ] **Step 1: Write the failing test**

```python
def test_hello_builds_and_runs(tmp_path):
    out = str(tmp_path / "hello")
    driver.build("examples/hello.logos", out)
    r = subprocess.run([out], capture_output=True, text=True)
    assert r.returncode == 0
    assert r.stdout == "hello\n"
```

- [ ] **Step 2: Run it**, expect FAIL with `no build() yet`.

- [ ] **Step 3: Implement IR, lowering for integer literals, `ret`, `let`, calls, and `(print ...)` / `(println ...)` mapped to the runtime**

`println` on a string literal emits a private global with explicit byte length and calls `rt_write(1, ptr, len)` then `rt_write(1, "\n", 1)`.

- [ ] **Step 4: Verify by reading the emitted IR**

Run: `python3 -m logos build examples/hello.logos -o build/hello && grep -n 'define\|declare' build/hello.ll | head`
Expected: `define i32 @main()`, `declare i64 @write(i32, ptr, i64)`.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: lower to IR, emit LLVM text, build first native binary"
```

---

### Task 6: Control flow

**Files:**
- Modify: `logos/check.py`, `logos/lower.py`, `logos/llvm.py`
- Test: `tests/exec/loops.logos`, `tests/exec/match.logos`, `tests/type/divzero.logos`

**Interfaces:**
- Produces: support for `(if c then else)`, `(while c body...)`, `(match e (pat body...)...)` over `:i64` literal patterns, `(break)`, `(continue)`, `(= a b)` and `(< > <= >= !=)` returning `:bool`, `(and a b)`, `(or a b)`, `(not a)`.
- Produces: `rt_panic(path, msg)` calls emitted for division and modulo by a runtime value.

- [ ] **Step 1: Write failing exec tests**

```
(test fizzbuzz
  (expect (out "1\n2\nfizz\n4\n...") (code 0))
  (letmut i :i64 1)
  (while (< i 16)
    (println (match (% i 3) 0 "fizz" _ "n")))
  (set i (+ i 1)))
```

and the review-focus test:

```
(test div-by-zero-panics
  (expect (code 2) (err-prefix "/: division by zero"))
  (println (/ 1 (deref zero))))
```

- [ ] **Step 2: Implement, run `python3 -m logos test tests/exec/loops.logos`**, expect PASS at `-O0` and `-O2`.

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m "feat: if, while, match, comparisons and checked division"
```

---

### Task 7: Structs and fixed arrays

**Files:**
- Modify: `logos/check.py`, `logos/lower.py`, `logos/llvm.py`
- Test: `tests/exec/structs.logos`, `tests/test_layout.py`

**Interfaces:**
- Produces: `(type point (struct (x :i64) (y :i64)))`, `(make point 1 2)`, `(get p x)`, `(set-field p x 5)`.
- Produces: `logos.llvm.struct_llty(name: str) -> str` emitting C layout: fields in declaration order, natural alignment, `%struct.point = type { i64, i64 }`.
- Produces: error code `E0200` unknown field, `E0201` field type mismatch.

- [ ] **Step 1: Write a test that pins layout**

```python
def test_struct_layout_matches_c():
    # (type pair (struct (a :u8) (b :i64)))
    assert emitted_type("pair") == "%struct.pair = type { i8, i64 }"
```

- [ ] **Step 2: Implement and run `tests/exec/structs.logos`**, which fills a struct, prints both fields, and checks a nested struct.

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m "feat: structs with C layout and fixed arrays"
```

---

### Task 8: Memory, ownership moves and the leak tracker

**Files:**
- Modify: `logos/check.py`, `logos/lower.py`, `logos/llvm.py`, `runtime/rt.c`, `runtime/rt.h`
- Test: `tests/mem/leak.logos`, `tests/mem/double-free.logos`, `tests/type/move.logos`

**Interfaces:**
- Produces: runtime `void *rt_alloc(size_t n, size_t align, const char *path);`, `void rt_free(void *p);`, `size_t rt_leak_count(void);`, `void rt_leak_report(void);`. Debug builds call `rt_leak_report` at exit of `main`.
- Produces: `(alloc :T n)`, `(free p)`, `(ptr p)`, `(deref p)`, `(set-deref p v)`, `(ptr-add p n)`.
- Produces: error codes `E0300` use after move, `E0301` free of a non-owning pointer, `E0302` move out of immutable binding.
- Produces: runtime panic on double free: message `double free`, exit code 2.

- [ ] **Step 1: Write the failing tests**

```
(test no-leak
  (expect (code 0) (leaks 0))
  (let p (alloc :i64 4))
  (set-deref p 0 7)
  (println (deref p))
  (free p))
```

and the move error case in `tests/type/move.logos` expecting `E0300`.

- [ ] **Step 2: Implement the tracker as a hash map keyed by pointer** (open addressing, `rt.h`), plus `rt_alloc`/`rt_free`.

- [ ] **Step 3: Prove the double free panics**

Run: `python3 -m logos test tests/mem/double-free.logos`
Expected: exit code 2, stderr contains `double free`.

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "feat: manual allocation, move checking, leak and double-free detection"
```

---

### Task 9: Strings, vectors and formatting

**Files:**
- Modify: `runtime/rt.c`, `runtime/rt.h`, `logos/check.py`, `logos/lower.py`
- Test: `tests/exec/strings.logos`, `tests/exec/vec.logos`

**Interfaces:**
- Produces: runtime `void rt_print_str(const unsigned char *p, size_t n);`, `void rt_print_i64(long long v);`, `void rt_print_f64(double v);`, `void rt_println(void);`, and `typedef struct { unsigned char *data; size_t len, cap; } rt_str;` with `rt_str_concat`, `rt_str_eq`, `rt_str_from_i64`.
- Produces: `(str-len s)`, `(str-concat a b)`, `(str-eq a b)`, `(str-slice s start end)`.
- Produces: `(vec :T)`, `(vec-push v x)`, `(vec-len v)`, `(vec-get v i)`, `(vec-free v)`; `(arr-get a i)`, `(arr-set a i v)`.
- Produces: error code `E0400` index type mismatch, `E0401` string not a str.

- [ ] **Step 1: Write the truncation test first, because it is the top failure mode**

```
(test multiline
  (expect (out "line one\nline two\n") (code 0))
  (println "line one\nline two"))
```

A second case prints a 300-byte string and compares byte count.

- [ ] **Step 2: Implement, then run both optimisations**

Run: `python3 -m logos test tests/exec/strings.logos --opt -O0 -O2`
Expected: PASS in both.

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m "feat: fat strings, vectors, integer and float formatting"
```

---

### Task 10: Generics and closures

**Files:**
- Modify: `logos/check.py`, `logos/lower.py`, `logos/llvm.py`
- Test: `tests/generics/`, `tests/exec/closures.logos`

**Interfaces:**
- Produces: type variables `:T` resolved per call site; monomorphised function names `map$i64$i64`.
- Produces: error code `E0500` instantiation limit reached at 256, `E0501` unbound type variable.
- Produces: `(fn ...)` expressions producing `FnTy`, called indirectly with an environment pointer.

- [ ] **Step 1: Write the failing test** `tests/generics/limit.logos` generating 257 call sites, expecting `E0500`, and `tests/exec/closures.logos` capturing one integer and returning it.

- [ ] **Step 2: Implement monomorphisation as a worklist over call sites**, so a generic that calls another generic terminates.

- [ ] **Step 3: Run the whole suite**, expect no regression in Tasks 5 to 9.

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "feat: monomorphised generics and closures"
```

---

### Task 11: Results, optionals and try

**Files:**
- Modify: `logos/check.py`, `logos/lower.py`, `logos/llvm.py`
- Test: `tests/exec/results.logos`, `tests/type/try-mismatch.logos`

**Interfaces:**
- Produces: `(type err (enum (bad :i64) (eof)))`, `(ok v)`, `(err e)`, `(some v)`, `(none)`, `(try e)`, exhaustive `match` on `(res ...)` and `(opt ...)`.
- Produces: error codes `E0600` non-exhaustive match, `E0601` try type mismatch (caller error type differs), `E0602` unhandled result.

- [ ] **Step 1: Write the failing tests**, one for a handled result, one for a `try` whose error types differ and must report `E0601`.

- [ ] **Step 2: Implement `res` as `{i1, T, E}` and `opt` as `{i1, T}`**, plus exhaustiveness checking on enum tags.

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m "feat: result and optional types with try and exhaustiveness"
```

---

### Task 12: Threads, mutexes, channels and atomics

**Files:**
- Modify: `runtime/rt.c`, `runtime/rt.h`, `logos/check.py`, `logos/lower.py`
- Test: `tests/thread/sum.logos`, `tests/thread/chan.logos`

**Interfaces:**
- Produces: `(spawn f args...) -> thread`, `(join t)`, `(mutex)`/`(lock m)`/`(unlock m)`, `(chan :T n)`/`(send c v)`/`(recv c)`, `(atomic :i64 v)`/`(atomic-add a n)`/`(atomic-load a)`, `(shared T)`.
- Produces: runtime over pthreads: `rt_thread_new`, `rt_join`, `rt_mutex_*`, `rt_chan_*`.
- Produces: error code `E0700` shared state not marked `(shared ...)`.

- [ ] **Step 1: Write the stress test**

```
(test eight-threads
  (expect (out "800000\n") (code 0))
  (letmut total (atomic :i64 0))
  (letmut ts (vec :thread))
  (letmut i :i64 0)
  (while (< i 8)
    (vec-push ts (spawn worker total))
    (set i (+ i 1)))
  (while (> (vec-len ts) 0) (join (vec-get ts 0)) (vec-pop ts))
  (println (atomic-load total)))
```

- [ ] **Step 2: Run it 1000 times**

Run: `for i in $(seq 1000); do python3 -m logos test tests/thread/sum.logos || break; done`
Expected: no failure in 1000 runs.

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m "feat: threads, mutex, channel, atomics over pthreads"
```

---

### Task 13: The GCC-style test harness

**Files:**
- Create: `logos/tester.py`, `tests/run.py`
- Modify: `logos/cli.py`
- Test: `tests/test_harness.py`

**Interfaces:**
- Produces: `logos.tester.run_suite(root: str, only: str | None, opts: list[str]) -> Summary` with `Summary(passed: int, failed: int, failures: list[tuple[str, str]], leaks: int)`.
- Produces: directive nodes `(expect (out S) (code N) (leaks N) (err-prefix S))`, module-level `(expect-error E0xxx)`, and the categories `parse type exec mem thread generics regress diff`.
- Produces: `tests/summary.txt` baseline, one line per case, format `<status> <category>/<case> <detail>`.
- Produces: CLI `python3 -m logos test [--only CAT] [--opt -O0 -O2] [--update-baseline]`.

- [ ] **Step 1: Write the failing test** — a harness run over `tests/exec` with a deliberately broken expectation must report 1 failure and exit non-zero.

- [ ] **Step 2: Implement the runner**: for each `.logos` file, parse; if the module declares `(expect-error E0xxx)`, expect that code from `check_module`; otherwise, for each `(test ...)` node, build with `run_test`, execute with a 10 second timeout, compare stdout, exit code and leak count.

- [ ] **Step 3: Add optimisation parity**: every exec case runs at `-O0` and `-O2` and both results must match the directive.

- [ ] **Step 4: Prove the baseline catches a regression**

Run: `python3 -m logos test --update-baseline && python3 -m logos test` then break one expectation and confirm exit code 1 naming the case.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: directive-driven harness with categories, parity and baseline"
```

---

### Task 14: Differential oracle, example programs, acceptance

**Files:**
- Create: `tests/diff/*.logos` and matching `*.c`, `examples/wc.logos`, `examples/json.logos`, `examples/matrix.logos`, `tools/accept.sh`
- Test: `tests/diff/run_diff.sh`

**Interfaces:**
- Produces: `tools/accept.sh` running, in order: `python3 -m logos test`, `bash tests/diff/run_diff.sh`, the three examples against `wc -c -w -l`, `jq` and a C reference build, and `logosc fmt` idempotency over the repo.
- Produces: `tests/diff/run_diff.sh` compiling each `X.logos` and `X.c` with clang, running both, comparing stdout and exit code, restricted to operations C defines.

- [ ] **Step 1: Write three diff cases** — integer arithmetic in range, string output, array iteration — each with its C twin.

- [ ] **Step 2: Write `examples/wc.logos`**, counting bytes, words and lines from stdin, and compare against `wc -c -w -l` on the repository README.

- [ ] **Step 3: Write `examples/matrix.logos`**, multiplying two 200x200 matrices with 4 threads, and compare against a C reference build.

- [ ] **Step 4: Run the acceptance script**

Run: `bash tools/accept.sh`
Expected: every section prints PASS and the script exits 0.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: differential oracle, example programs and acceptance script"
```

---

## Self-review

- Spec coverage: intent (Tasks 1 to 5), canonical form and printer (2), addressing (2, 3), checker (4, 7, 8, 10, 11), arithmetic (6), memory (8), strings (9), concurrency (12), tests as grammar (13), GCC-style suite (13, 14), acceptance criteria (14). Every non-goal is absent by construction.
- Types and names: `Node.path` is used identically in Tasks 2, 3, 4 and 13. `driver.build` signature is fixed in Task 5 and used unchanged in 13. Runtime symbols defined in Task 8 are used unchanged in 9 and 12.
- Review Focus: each of the five lines has an owning test — Task 9 (truncation), Task 6 (division), Task 8 (double free), Task 13 (parity), Task 12 (determinism).
- Left to the builder: exact IR instruction set, stdlib breadth beyond the named calls, fuzz generator.
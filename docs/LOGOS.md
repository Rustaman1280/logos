# Logos: the generated manual

A prefix-form language for AI readers, compiled by `logosc` to native x86_64 Linux binaries. Generated from the compiler's tables and `docs/manual/*.logos`; do not edit.

## Cheat sheet

A program is one file and one module: `(mod name)`, then `(fn name :ret ((p :type) ...) body...)` definitions, `(type ...)` declarations, and `(test name (expect (out "...") (code 0)) body...)` cases.

```logos
(mod hello)

(fn add :i64 ((a :i64) (b :i64))
  (ret (+ a b)))

(test add
  (expect (out "3\n") (code 0))
  (println (add 1 2)))
```

The ten rules that decide whether code compiles:

1. One canonical spelling for every construct.
2. No implicit conversion; `(cast T e)` is the only one.
3. A binding is block-scoped: function body, if branch, while body, match arm.
4. A vector or owner-containing value moves; it is never copied.
5. A value cannot be read out of a place when it owns memory.
6. A release inside a loop is refused for a binding declared outside it.
7. A capturing closure cannot cross a call boundary.
8. A result must be handled: matched, tried, returned or passed on.
9. Shared state must carry `(shared ...)`.
10. A form the checker accepts, the backend can emit.

Machine-readable: `logosc guide --json` prints the manifest (forms, types, operators, builtins, reserved, codes, categories). `logosc guide --check` proves this file matches the compiler.

## Manual

17 chapters in learning order. Each is a complete module; its `(test ...)` node declares the output shown, and `python3 -m logos test docs/manual` runs every one at `-O0` and `-O2`.

### 1. Values and literals

```logos
(mod values)

(test literals
  (expect (out "42\n-7\n255\n3.5\n2\n1e+300\ntrue\nfalse\nline one\nline two\n5\n")
    (code 0)
    (leaks 0))
  (let i :i64 42)
  (println i)
  (println -7)
  (let small :u8 255)
  (println (cast :i64 small))
  (println 3.5)
  (println 2.0)
  (println 1e300)
  (println true)
  (println false)
  (print "line one\n")
  (println "line two")
  (let unicode "é→")
  (println (str-len unicode)))
```

Expected stdout, exit 0:

```text
42
-7
255
3.5
2
1e+300
true
false
line one
line two
5
```

### 2. let, letmut and set

```logos
(mod bindings)

(test bindings
  (expect (out "11\n1\n2\n1\n0\n2\n4\n3\n")
    (code 0)
    (leaks 0))
  (letmut n :i64 10)
  (set n (+ n 1))
  (println n)
  (let one :i64 1)
  (println one)
  (println (match 0 (0 (let one :i64 2) one) (_ 0)))
  (println one)
  (letmut i :i64 0)
  (while (< i 3)
    (let doubled :i64 (* i 2))
    (println doubled)
    (set i (+ i 1)))
  (println i))
```

Expected stdout, exit 0:

```text
11
1
2
1
0
2
4
3
```

### 3. Functions and ret

```logos
(mod functions)

(fn add :i64 ((a :i64) (b :i64))
  (ret (+ a b)))

(fn greet :str ((name :str))
  (ret (str-concat "hi " name)))

(fn announce :void ((n :i64))
  (println n))

(test calls
  (expect (out "7\nhi ai\n5\n")
    (code 0)
    (leaks 0))
  (println (add 3 4))
  (println (greet "ai"))
  (announce 5))
```

Expected stdout, exit 0:

```text
7
hi ai
5
```

### 4. if, while and match

```logos
(mod control)

(fn classify :str ((n :i64))
  (ret (match n (0 "zero") (-1 "minus") (_ "many"))))

(test control
  (expect (out "small\n0\n1\n2\nzero\nminus\nmany\n")
    (code 0)
    (leaks 0))
  (println (if (< 1 2) "small" "big"))
  (letmut i :i64 0)
  (while (< i 3)
    (println i)
    (set i (+ i 1)))
  (println (classify 0))
  (println (classify -1))
  (println (classify 9)))
```

Expected stdout, exit 0:

```text
small
0
1
2
zero
minus
many
```

### 5. Structs

```logos
(mod structs)

(type point (struct (x :i64) (y :i64)))

(type segment (struct (from point) (to point)))

(fn total :i64 ((p point))
  (ret (+ (get p x)
         (get p y))))

(test structs
  (expect (out "3\n7\n7\n")
    (code 0)
    (leaks 0))
  (letmut p point (make point 1 2))
  (println (total p))
  (set-field p x 5)
  (println (total p))
  (let s segment (make segment
                   (make point 1 2)
                   (make point 3 4)))
  (let end-point (get s to))
  (println (total end-point)))
```

Expected stdout, exit 0:

```text
3
7
7
```

### 6. Fixed arrays

```logos
(mod arrays)

(type point (struct (x :i64) (y :i64)))

(test arrays
  (expect (out "3\n9\n4\n")
    (code 0)
    (leaks 0))
  (letmut a
    (arr :i64 3)
    (make (arr :i64 3) 1 2 3))
  (println (arr-get a 2))
  (arr-set a 0 9)
  (println (arr-get a 0))
  (letmut ps
    (arr point 2)
    (make (arr point 2)
      (make point 1 2)
      (make point 3 4)))
  (println (get (arr-get ps 1) y)))
```

Expected stdout, exit 0:

```text
3
9
4
```

### 7. Strings

```logos
(mod strings)

(test strings
  (expect (out "5\nhello, ai\nell\nabcdef\n6\n5\ntrue\nfalse\ntrue\n")
    (code 0)
    (leaks 0))
  (println (str-len "hello"))
  (println (str-concat "hello" ", ai"))
  (println (str-slice "hello" 1 4))
  (println "abcdef")
  (println (str-len "héllo"))
  (println (str-len "é→"))
  (println (= "abc" "abc"))
  (println (= "abc" "abd"))
  (println (str-eq (str-slice "abcdef" 1 4) "bcd")))
```

Expected stdout, exit 0:

```text
5
hello, ai
ell
abcdef
6
5
true
false
true
```

### 8. Vectors

```logos
(mod vectors)

(fn sum :i64 ((v (vec :i64)))
  (letmut i :i64 0)
  (letmut total :i64 0)
  (while (< i (vec-len v))
    (set total (+ total (vec-get v i)))
    (set i (+ i 1)))
  (vec-free v)
  (ret total))

(test vectors
  (expect (out "7\n3\n12\n")
    (code 0)
    (leaks 0))
  (letmut v
    (vec :i64)
    (make (vec :i64)))
  (vec-push v 1)
  (vec-push v 2)
  (vec-push v 3)
  (vec-set v 0 7)
  (println (vec-get v 0))
  (println (vec-len v))
  (println (sum v)))
```

Expected stdout, exit 0:

```text
7
3
12
```

### 9. alloc, free, own and ptr

```logos
(mod memory)

(test memory
  (expect (out "9\n1\n2\n5\n")
    (code 0)
    (leaks 0))
  (letmut p (alloc :i64 3))
  (set-deref p 9)
  (set-deref (ptr-add p 1) 1)
  (set-deref (ptr-add (ptr p) 2) 2)
  (println (deref p))
  (println (deref (ptr-add p 1)))
  (println (deref (ptr-add (ptr p) 2)))
  (letmut q (alloc :i64 1))
  (set-deref q 5)
  (letmut r q)
  (println (deref r))
  (free r)
  (free p))
```

Expected stdout, exit 0:

```text
9
1
2
5
```

### 10. Results and try

```logos
(mod results)

(type parse-error (enum (bad :i64) (empty)))

(fn checked :
  (res :i64 parse-error) ((n :i64))
  (if (> n 0)
    (ret (ok n))
    (ret (err (bad n)))))

(fn doubled :
  (res :i64 parse-error) ((n :i64))
  (ret (ok (* 2 (try (checked n))))))

(test results
  (expect (out "42\n-1\n")
    (code 0)
    (leaks 0))
  (match (doubled 21) ((ok v) (println v)) ((err _) (println 0)))
  (match (doubled -1) ((ok v) (println v)) ((err e) (match e ((bad n) (println n)) (empty (println 0))))))
```

Expected stdout, exit 0:

```text
42
-1
```

### 11. Enums

```logos
(mod enums)

(type shape (enum (circle :i64) (square :i64) (dot)))

(fn area :i64 ((s shape))
  (ret (match s ((circle r) (* r r 3)) ((square n) (* n n)) (dot 0))))

(test enums
  (expect (out "12\n9\n0\n")
    (code 0)
    (leaks 0))
  (println (area (circle 2)))
  (println (area (square 3)))
  (println (area dot)))
```

Expected stdout, exit 0:

```text
12
9
0
```

### 12. Optionals

```logos
(mod optionals)

(fn find :
  (opt :i64) ((v :i64))
  (if (> v 0)
    (ret (some v))
    (ret (none))))

(test optionals
  (expect (out "5\n0\n")
    (code 0)
    (leaks 0))
  (match (find 5) ((some v) (println v)) ((none) (println 0)))
  (match (find -1) ((some v) (println v)) ((none) (println 0))))
```

Expected stdout, exit 0:

```text
5
0
```

### 13. Generics

```logos
(mod generics)

(fn id :T ((x :T))
  (ret x))

(fn first :T ((a :T) (b :U))
  (ret a))

(fn twice :i64 ((n :i64))
  (ret (* 2 (id n))))

(test generics
  (expect (out "42\n3\n7\nx\n42\n")
    (code 0)
    (leaks 0))
  (println (id 42))
  (println (str-len (id "abc")))
  (println (first 7 "x"))
  (println (first "x" 7))
  (println (twice 21)))
```

Expected stdout, exit 0:

```text
42
3
7
x
42
```

### 14. Closures

```logos
(mod closures)

(fn apply :U ((f (fn (:T) :U)) (x :T))
  (ret (f x)))

(test closures
  (expect (out "10\n12\n8\n15\n")
    (code 0)
    (leaks 0))
  (let k 10)
  (let read (fn :i64 ()
              (ret k)))
  (println (read))
  (println (apply (fn :i64 ((n :i64))
                    (ret (* n 3))) 4))
  (let add (fn :i64 ((n :i64))
             (ret (+ n k))))
  (println (add -2))
  (println (add 5)))
```

Expected stdout, exit 0:

```text
10
12
8
15
```

### 15. Threads

```logos
(mod threads)

(fn worker :void ((total (shared (atomic :i64))) (n :i64))
  (letmut i :i64 0)
  (while (< i n)
    (atomic-add total 1)
    (set i (+ i 1))))

(test threads
  (expect (out "4000\n")
    (code 0)
    (leaks 0))
  (letmut total (atomic :i64 0))
  (let a (spawn worker total 1000))
  (let b (spawn worker total 1000))
  (let c (spawn worker total 1000))
  (let d (spawn worker total 1000))
  (join a)
  (join b)
  (join c)
  (join d)
  (println (atomic-load total)))
```

Expected stdout, exit 0:

```text
4000
```

### 16. extern

```logos
(mod cextern)

(extern fn getpid :i64 ())

(extern fn write :i64 ((fd :i64) (text :str)))

(test extern
  (expect (out "pid ok\nhi2\n")
    (code 0)
    (leaks 0))
  (if (> (getpid) 0)
    (println "pid ok")
    (println "pid bad"))
  (println (write 1 "hi")))
```

Expected stdout, exit 0:

```text
pid ok
hi2
```

### 17. Tests

```logos
(mod testing)

(fn square :i64 ((n :i64))
  (ret (* n n)))

(test square
  (expect (out "9\n")
    (code 0)
    (leaks 0))
  (println (square 3)))
```

Expected stdout, exit 0:

```text
9
```

## Reference

### The ten rules, and where the compiler enforces them

| # | Rule | Enforced by |
| --- | --- | --- |
| 1 | One canonical spelling for every construct. | `logos.fmt` gives every node one printed shape, the parser accepts no aliases, and an unknown head is E0002. |
| 2 | No implicit conversion; `(cast T e)` is the only one. | `types.compatible` requires the same type exactly, so a mixed width or kind is E0100. `cast` converts numbers only. |
| 3 | A binding is block-scoped: function body, if branch, while body, match arm. | `_Env.block()` pushes one scope per block; a name declared inside is gone at the closing parenthesis. |
| 4 | A vector or owner-containing value moves; it is never copied. | `_move_only` and `_consume`: binding it or passing it moves it, and a second use is E0300 or E0402. |
| 5 | A value cannot be read out of a place when it owns memory. | A struct field, an array or vector element and a dereference of an owning type are E0300/E0402 (`_OWNER_PLACE`, `_VEC_PLACE`). |
| 6 | A release inside a loop is refused for a binding declared outside it. | E0303 `LOOP_MOVE`: the body is checked once, so a second pass would read a slot the first emptied. |
| 7 | A capturing closure cannot cross a call boundary. | E0502 `CLOSURE_ESCAPE`: its environment is a slot in the creating frame, and `(fn* ...)` is not `(fn ...)`. |
| 8 | A result must be handled: matched, tried, returned or passed on. | E0602 `UNHANDLED_RESULT` at the binding whose `(res T E)` is dropped in statement position. |
| 9 | Shared state must carry `(shared ...)`. | E0700 `SHARED_MARKER` at the declaration and at the thread boundary, checked through aggregates. |
| 10 | A form the checker accepts, the backend can emit. | Every checker branch has a lowerer branch; a form the backend cannot emit is a compiler bug, never a runtime surprise. |

### Forms

| Form | Arity | Meaning |
| --- | --- | --- |
| `mod` | 1 | `(mod name)` names the module; one per file |
| `use` | 1 | `(use name)` imports `name.logos` beside this file |
| `fn` | 2+ | `(fn name :T ((p :T) ...) body...)` a function, `(fn :T ((p :T) ...) body...)` a closure, `(fn (T...) R)` a function type |
| `type` | 2 | `(type name (struct ...))` or `(type name (enum ...))` |
| `extern` | 2+ | `(extern fn name :T ((p :T) ...))` declares a C symbol |
| `test` | 1+ | `(test name (expect ...) body...)` one runnable case |
| `expect` | 1+ | `(expect (out S) (code N) (leaks N) (err-contains S))` |
| `expect-error` | 1 | `(expect-error E0xxx)` declares a negative case |
| `struct` | 0+ | `(struct (field T) ...)` the fields of a named type |
| `enum` | 1+ | `(enum (variant) (variant T) ...)` the variants |
| `let` | 2-3 | `(let name [type] value)` an immutable binding |
| `letmut` | 2-3 | `(letmut name [type] value)` a mutable binding |
| `set` | 2 | `(set name value)` writes a `letmut` binding |
| `ret` | 0-1 | `(ret [value])` returns from the enclosing function |
| `if` | 3 | `(if cond then else)`; both arms have one type |
| `while` | 1+ | `(while cond body...)`; `break` and `continue` leave it |
| `match` | 2+ | `(match value arm...)`; closed sets must be exhaustive |
| `break` | 0 | `(break)` leaves the innermost loop |
| `continue` | 0 | `(continue)` starts the next iteration |
| `make` | 1+ | `(make T value...)` builds a struct, array or vector |
| `get` | 2 | `(get value field)` reads a struct field |
| `set-field` | 3 | `(set-field name field value)` writes a field |
| `arr-get` | 2 | `(arr-get a i)` reads a fixed-array element |
| `arr-set` | 3 | `(arr-set name i value)` writes a fixed-array element |
| `str-len` | 1 | `(str-len s)` the byte length of a `:str` |
| `str-concat` | 2 | `(str-concat a b)` allocates a new joined string |
| `str-eq` | 2 | `(str-eq a b)` byte equality of two strings |
| `str-slice` | 3 | `(str-slice s start end)` bytes `[start, end)` |
| `read-all` | 0 | `(read-all)` reads standard input to a `:str` |
| `vec-push` | 2 | `(vec-push v x)` appends, growing the buffer |
| `vec-len` | 1 | `(vec-len v)` the element count |
| `vec-get` | 2 | `(vec-get v i)` reads an element, bounds checked |
| `vec-set` | 3 | `(vec-set v i x)` writes an element, bounds checked |
| `vec-free` | 1 | `(vec-free v)` releases a vector's buffer |
| `alloc` | 2 | `(alloc T n)` allocates n values of T, uninitialised |
| `free` | 1 | `(free p)` releases an `(own T)` |
| `ptr` | 1 | `(ptr p)` the raw address of an owner, as `(ptr T)` |
| `deref` | 1 | `(deref p)` reads the value a pointer addresses |
| `set-deref` | 2 | `(set-deref p x)` writes through a pointer |
| `ptr-add` | 2 | `(ptr-add p n)` the address n elements further on |
| `+` | 2+ | add two or more numbers of one type |
| `-` | 2+ | subtract numbers of one type |
| `*` | 2+ | multiply numbers of one type |
| `/` | 2+ | divide numbers of one type |
| `%` | 2 | integer remainder; floats are E0100 |
| `<` | 2 | less than, yielding `:bool` |
| `<=` | 2 | less than or equal, yielding `:bool` |
| `>` | 2 | greater than, yielding `:bool` |
| `>=` | 2 | greater than or equal, yielding `:bool` |
| `=` | 2 | equal, yielding `:bool`; `:str` compares bytes |
| `!=` | 2 | not equal, yielding `:bool` |
| `shl` | 2 | shift left; the count keeps the value's width |
| `shr` | 2 | arithmetic shift right; the sign bit fills |
| `shr-u` | 2 | logical shift right; zeros fill |
| `and` | 2 | short-circuit boolean and; both operands `:bool` |
| `or` | 2 | short-circuit boolean or; both operands `:bool` |
| `not` | 1 | boolean negation |
| `cast` | 2 | `(cast T e)` the one explicit numeric conversion |
| `ok` | 1 | `(ok v)` builds the success side of a result |
| `err` | 1 | `(err e)` builds the error side of a result |
| `some` | 1 | `(some v)` builds a present optional |
| `none` | 0 | `(none)` builds an absent optional |
| `try` | 1 | `(try r)` propagates an error from a result |
| `spawn` | 1+ | `(spawn f arg...)` starts a thread running f |
| `join` | 1 | `(join t)` waits for a thread |
| `mutex` | 0 | `(mutex)` creates a `(shared :mutex)` lock |
| `lock` | 1 | `(lock m)` acquires a mutex |
| `unlock` | 1 | `(unlock m)` releases a mutex |
| `chan` | 1-2 | `(chan T cap)` creates a channel of T |
| `send` | 2 | `(send c x)` copies x into a channel, blocking when full |
| `recv` | 1 | `(recv c)` takes the next value, blocking when empty |
| `atomic` | 1-2 | `(atomic T v)` creates an atomic cell holding v |
| `atomic-add` | 2 | `(atomic-add a n)` adds atomically |
| `atomic-load` | 1 | `(atomic-load a)` reads atomically |
| `atomic-store` | 2 | `(atomic-store a n)` writes atomically |
| `arr` | 2 | `(arr T n)` a fixed array of n values of T |
| `vec` | 1 | `(vec T)` a growable array owned by a three-word descriptor |
| `own` | 1 | `(own T)` an owning pointer, released by `free` |
| `fn*` | 2 | `(fn* (T...) R)` a capturing closure, confined to its frame |
| `res` | 2 | `(res T E)` an ok T or an error E; must be handled |
| `opt` | 1 | `(opt T)` some T or none |
| `shared` | 1 | `(shared T)` state two threads may touch; required at a boundary |

### Primitive types

| Type | Kind | Meaning |
| --- | --- | --- |
| `:i8` | integer | a signed 8-bit integer |
| `:i16` | integer | a signed 16-bit integer |
| `:i32` | integer | a signed 32-bit integer |
| `:i64` | integer | a signed 64-bit integer |
| `:u8` | integer | an unsigned 8-bit integer |
| `:u16` | integer | an unsigned 16-bit integer |
| `:u32` | integer | an unsigned 32-bit integer |
| `:u64` | integer | an unsigned 64-bit integer |
| `:f32` | float | an IEEE 32-bit floating-point number |
| `:f64` | float | an IEEE 64-bit floating-point number |
| `:bool` | boolean | `true` or `false` |
| `:str` | string | a UTF-8 byte string: a pointer and a byte length |
| `:void` | unit | no value; the type of a statement form |
| `:thread` | handle | a spawned thread handle |
| `:mutex` | handle | a lock handle, always `(shared :mutex)` when made |

### Operators

| Operator | Arity | Meaning |
| --- | --- | --- |
| `+` | 2+ | add numbers of one type |
| `-` | 2+ | subtract numbers of one type |
| `*` | 2+ | multiply numbers of one type |
| `/` | 2+ | divide numbers of one type |
| `%` | 2+ | integer remainder; floats are E0100 |
| `<` | 2 | ordered compare, yielding `:bool` |
| `<=` | 2 | ordered compare, yielding `:bool` |
| `>` | 2 | ordered compare, yielding `:bool` |
| `>=` | 2 | ordered compare, yielding `:bool` |
| `=` | 2 | equality over :i8, :i16, :i32, :i64, :u8, :u16, :u32, :u64, :f32, :f64, :bool, :str; `:str` compares bytes |
| `!=` | 2 | equality over :i8, :i16, :i32, :i64, :u8, :u16, :u32, :u64, :f32, :f64, :bool, :str; `:str` compares bytes |
| `shl` | 2 | shift left |
| `shr` | 2 | arithmetic shift right |
| `shr-u` | 2 | logical shift right |
| `and` | 2 | short-circuit `:bool` operator |
| `or` | 2 | short-circuit `:bool` operator |
| `not` | 1 | `:bool` negation |

### Builtins

| Builtin | Meaning |
| --- | --- |
| `print` | print a value to stdout with no newline |
| `println` | print a value to stdout followed by a newline |

### Reserved names

A definition may not take a name a form already spells (E0003). The reserved forms are: `alloc`, `and`, `break`, `cast`, `continue`, `deref`, `err`, `free`, `get`, `if`, `join`, `let`, `letmut`, `lock`, `make`, `match`, `none`, `not`, `ok`, `or`, `print`, `println`, `ptr`, `ptr-add`, `recv`, `ret`, `send`, `set`, `set-deref`, `set-field`, `shl`, `shr`, `shr-u`, `some`, `spawn`, `try`, `unlock`, `while`.

Reserved form prefixes: `vec-`, `str-`, `arr-`, `atomic-`. Reserved enum variant names: `ok`, `err`, `some`, `none`, `try`, `print`, `println`, `spawn`, `join`, `mutex`, `lock`, `unlock`, `chan`, `send`, `recv`, `atomic`, `atomic-add`, `atomic-load`, `atomic-store`.

### Diagnostic codes

| Code | Meaning |
| --- | --- |
| `E0001` | unbalanced parentheses |
| `E0002` | unknown node |
| `E0003` | duplicate definition |
| `E0004` | circular import |
| `E0005` | name not found |
| `E0006` | lexical error |
| `E0100` | type mismatch |
| `E0101` | unknown type |
| `E0102` | wrong arity |
| `E0103` | unknown function |
| `E0104` | not mutable |
| `E0105` | missing return |
| `E0106` | integer literal out of range |
| `E0107` | unused binding |
| `E0200` | unknown field |
| `E0201` | field type mismatch |
| `E0300` | use after move |
| `E0301` | free of a non-owning pointer |
| `E0302` | move out of an immutable binding |
| `E0303` | move of a binding declared outside a loop |
| `E0400` | index type mismatch |
| `E0401` | string not a str |
| `E0402` | vector value not copyable |
| `E0500` | instantiation limit reached |
| `E0501` | unbound type variable |
| `E0502` | closure escapes its function |
| `E0600` | non-exhaustive match |
| `E0601` | try type mismatch |
| `E0602` | unhandled result |
| `E0700` | shared state not marked |

Runtime panic codes: `R0001` division by zero, `R0003` failed runtime check, `R0004` free of an unknown address, `R0005` thread operation failed. `R0002` double free is a runtime name only.

### Test categories

`logosc test --only <category>` runs one: `parse`, `type`, `exec`, `mem`, `thread`, `generics`, `regress`, `diff`.

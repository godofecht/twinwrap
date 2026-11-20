import pytest

from twinwrap.sig import LengthExpr, SignatureError, c_prototype, parse_signature


def test_scalar_and_slice():
    s = parse_signature("fn mean(xs: [f64]) -> f64")
    assert c_prototype(s) == "double twinwrap_mean(const double *xs_ptr, int32_t xs_len)"


def test_returned_array_adds_out_buffer():
    s = parse_signature("fn scale(xs: [f64], k: f64) -> [f64; len(xs)]")
    assert c_prototype(s) == (
        "void twinwrap_scale(const double *xs_ptr, int32_t xs_len, double k, double *out_ptr, int32_t out_len)"
    )


def test_returned_str():
    s = parse_signature("fn shout(s: str) -> str")
    assert c_prototype(s) == "uint8_t *twinwrap_shout(const uint8_t *s_ptr, int32_t s_len, int32_t *out_len)"


def test_no_return():
    assert c_prototype(parse_signature("fn reset()")) == "void twinwrap_reset(void)"


@pytest.mark.parametrize("bad", [
    "mean(xs: [f64])",                       # no fn
    "fn f(x: f128)",                         # unknown type
    "fn f(xs: [f64; 3])",                    # length on an input
    "fn f(xs: [f64]) -> [f64]",              # returned array without length
    "fn f(x: i32, x: i32)",                  # duplicate
    "fn f(xs: [bool])",                      # bool arrays
    "fn twinwrap_x()",                       # reserved name
    "fn f(out: i32)",                        # reserved parameter
    "fn f(xs: [f64]) -> [f64; len(ys)]",     # unknown parameter
    "fn f(k: f64) -> [f64; k]",              # float length
    "fn f(xs: [f64]) -> [f64; xs ** 2]",     # unsupported operator
    "fn f(xs: [f64]) -> [f64; __import__('os')]",
])
def test_rejected(bad):
    with pytest.raises(SignatureError):
        parse_signature(bad)


def test_length_expression_renders_for_both_languages():
    s = parse_signature("fn pairs(xs: [f64], n: i32) -> [f64; (len(xs) - 1) * n // 2]")
    e = LengthExpr(s.ret.length, s)
    py = e.render("py", lambda name, is_len: f"L({name})" if is_len else name)
    js = e.render("js", lambda name, is_len: f"L({name})" if is_len else name)
    assert eval(py, {"L": len, "xs": [1, 2, 3, 4, 5], "n": 3}) == 6
    assert "Math.floor(" in js

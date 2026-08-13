"""OMML -> LaTeX conversion.

Every case here is markup Word actually produces for the kinds of expressions a
Calculus / Statistics lecturer writes. Before this converter existed these all
vanished during ingestion, because python-docx's ``paragraph.text`` returns only
``w:t`` runs and skips ``m:oMath`` entirely.
"""

import pytest
from docx.oxml import parse_xml

from app.ai.ingestion.omml import omml_to_latex

NS = 'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"'


def conv(inner: str) -> str:
    return omml_to_latex(parse_xml(f"<m:oMath {NS}>{inner}</m:oMath>"))


def run(text: str) -> str:
    return f"<m:r><m:t>{text}</m:t></m:r>"


def test_fraction():
    xml = f"<m:f><m:num>{run('a')}</m:num><m:den>{run('b')}</m:den></m:f>"
    assert conv(xml) == r"\frac{a}{b}"


def test_superscript_keeps_single_char_unbraced():
    xml = f"<m:sSup><m:e>{run('x')}</m:e><m:sup>{run('2')}</m:sup></m:sSup>"
    assert conv(xml) == "x^2"


def test_subscript_and_superscript_together():
    xml = (
        f"<m:sSubSup><m:e>{run('A')}</m:e>"
        f"<m:sub>{run('i')}</m:sub><m:sup>{run('n')}</m:sup></m:sSubSup>"
    )
    assert conv(xml) == "A_i^n"


def test_definite_integral_with_beside_limits():
    xml = (
        '<m:nary><m:naryPr><m:chr m:val="∫"/><m:limLoc m:val="subSup"/></m:naryPr>'
        f"<m:sub>{run('0')}</m:sub><m:sup>{run('1')}</m:sup>"
        f"<m:e>{run('f(x) dx')}</m:e></m:nary>"
    )
    assert conv(xml) == r"\int_0^1 f(x) dx"


def test_summation_stacks_limits():
    xml = (
        '<m:nary><m:naryPr><m:chr m:val="∑"/><m:limLoc m:val="undOvr"/></m:naryPr>'
        f"<m:sub>{run('i=1')}</m:sub><m:sup>{run('n')}</m:sup>"
        f"<m:e>{run('x')}</m:e></m:nary>"
    )
    assert conv(xml) == r"\sum\limits_{i=1}^n x"


def test_nary_defaults_to_integral_when_char_omitted():
    xml = f"<m:nary><m:naryPr/><m:sub/><m:sup/><m:e>{run('dx')}</m:e></m:nary>"
    assert conv(xml).startswith(r"\int")


def test_square_root_hides_degree():
    xml = (
        '<m:rad><m:radPr><m:degHide m:val="1"/></m:radPr><m:deg/>'
        f"<m:e>{run('2')}</m:e></m:rad>"
    )
    assert conv(xml) == r"\sqrt{2}"


def test_nth_root_braces_its_argument():
    xml = f"<m:rad><m:deg>{run('3')}</m:deg><m:e>{run('x')}</m:e></m:rad>"
    assert conv(xml) == r"\sqrt[3]{x}"


def test_limit_renders_as_lim_command_not_operatorname():
    xml = (
        "<m:func><m:fName><m:limLow>"
        f"<m:e>{run('lim')}</m:e><m:lim>{run('x→a')}</m:lim>"
        f"</m:limLow></m:fName><m:e>{run('f(x)')}</m:e></m:func>"
    )
    assert conv(xml) == r"\lim_{x\to a} f(x)"


def test_named_function_becomes_latex_command():
    xml = f"<m:func><m:fName>{run('sin')}</m:fName><m:e>{run('x')}</m:e></m:func>"
    assert conv(xml) == r"\sin x"


def test_unknown_function_uses_operatorname():
    xml = f"<m:func><m:fName>{run('sgn')}</m:fName><m:e>{run('x')}</m:e></m:func>"
    assert r"\operatorname{sgn}" in conv(xml)


def test_delimiters_default_to_parentheses():
    xml = f"<m:d><m:e>{run('x')}</m:e></m:d>"
    assert conv(xml) == r"\left( x \right)"


def test_delimiters_honour_explicit_characters():
    xml = (
        '<m:d><m:dPr><m:begChr m:val="["/><m:endChr m:val="]"/></m:dPr>'
        f"<m:e>{run('a,b')}</m:e></m:d>"
    )
    assert conv(xml) == r"\left[ a,b \right]"


def test_nobar_fraction_is_a_binomial():
    xml = (
        '<m:f><m:fPr><m:type m:val="noBar"/></m:fPr>'
        f"<m:num>{run('n')}</m:num><m:den>{run('k')}</m:den></m:f>"
    )
    assert conv(xml) == r"\binom{n}{k}"


def test_accent_bar_produces_braced_argument():
    """Guards the \\barx regression: an unbraced single char glues to the command."""
    xml = f'<m:acc><m:accPr><m:chr m:val="̅"/></m:accPr><m:e>{run("x")}</m:e></m:acc>'
    assert conv(xml) == r"\bar{x}"


def test_matrix():
    xml = (
        f"<m:m><m:mr><m:e>{run('a')}</m:e><m:e>{run('b')}</m:e></m:mr>"
        f"<m:mr><m:e>{run('c')}</m:e><m:e>{run('d')}</m:e></m:mr></m:m>"
    )
    assert conv(xml) == r"\begin{matrix} a & b \\ c & d \end{matrix}"


@pytest.mark.parametrize(
    "char,expected",
    [
        ("μ", r"\mu"),
        ("σ", r"\sigma"),
        ("≤", r"\leq"),
        ("≠", r"\neq"),
        ("∞", r"\infty"),
        ("∂", r"\partial"),
        ("∈", r"\in"),
    ],
)
def test_unicode_symbols_map_to_latex(char, expected):
    assert expected in conv(run(char))


def test_latex_special_characters_are_escaped():
    assert r"\%" in conv(run("50%"))
    assert r"\_" in conv(run("a_b"))


def test_empty_equation_yields_empty_string():
    assert conv("") == ""


def test_conversion_never_raises_on_malformed_input():
    """Ingestion of a whole document must not abort because one equation is odd."""
    assert omml_to_latex(None) == ""


def test_normal_distribution_density():
    """The expression a Statistics lecturer is most likely to put on a slide."""
    xml = (
        "<m:f><m:num>" + run("1") + "</m:num><m:den>" + run("σ")
        + '<m:rad><m:radPr><m:degHide m:val="1"/></m:radPr><m:deg/>'
        + f"<m:e>{run('2π')}</m:e></m:rad></m:den></m:f>"
    )
    assert conv(xml) == r"\frac{1}{\sigma \sqrt{2\pi}}"

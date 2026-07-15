from config_review.cli import parse_args
from config_review.core import VERSION


def test_version():
    assert VERSION == "1.0.0"


def test_default_arguments():
    args = parse_args([])
    assert args.source is None
    assert args.target is None


def test_main_footer_never_exceeds_available_width():
    from config_review.tui import main_footer_lines

    for width in (28, 40, 49, 65, 100):
        lines = main_footer_lines(width)
        assert lines
        assert all(len(line) <= width for line in lines)
        assert any("config" in line for line in lines)
        assert any("help" in line for line in lines)
        assert any("quit" in line for line in lines)

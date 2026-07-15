from config_review.cli import parse_args
from config_review.core import VERSION


def test_version():
    assert VERSION == "1.0.0"


def test_default_arguments():
    args = parse_args([])
    assert str(args.source) == "dev"
    assert str(args.target) == "test"

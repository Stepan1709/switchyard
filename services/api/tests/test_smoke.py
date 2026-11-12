import app
from app.__main__ import main


def test_version_is_set() -> None:
    assert app.__version__.count(".") == 2


def test_main_prints_version(capsys) -> None:  # type: ignore[no-untyped-def]
    main()
    assert app.__version__ in capsys.readouterr().out

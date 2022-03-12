from pathlib import Path
from typing import List

import pytest

from conda_local import api
from conda_local.external import compare_records


def fetch_local_specs(channel) -> List[str]:
    channel = Path(channel)
    file = channel / "specs.txt"
    lines = file.read_text().split("\n")
    return lines


@pytest.mark.parametrize(
    "name",
    [
        "test01",
        "test02",
        "test03",
        "test04",
        "test05",
        "test06",
        "test07",
        "test08",
        "test09",
        "test10",
    ],
)
def test_query_of_packages(datadir, subdirs, name):
    base = datadir / name
    specs = fetch_local_specs(base)
    expected = api.iterate(str(base / "expected"), subdirs=subdirs)
    actual = api.query(str(base / "all"), specs, subdirs=subdirs)
    added, removed = compare_records(actual, expected)
    assert not added
    assert not removed
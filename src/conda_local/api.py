"""High-level api functions for conda-local."""

import shutil
import sys
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Set, Tuple, TypeVar, Union, cast

from tqdm import tqdm

from conda_local._typing import OneOrMoreStrings, PathOrString
from conda_local.deps import DependencyFinder
from conda_local.external import (
    PackageRecord,
    Spinner,
    UnavailableInvalidChannel,
    compare_records,
    download_package,
    download_patch_instructions,
    get_current_subdirs,
    iter_channels,
    update_index,
)

# from conda_local.spinner import Spinner

T = TypeVar("T", covariant=True)


def diff(
    local: PathOrString,
    upstream: OneOrMoreStrings,
    specs: OneOrMoreStrings,
    subdirs: Optional[OneOrMoreStrings] = None,
) -> Tuple[Set[PackageRecord], Set[PackageRecord]]:
    """Computes the difference between local and upstream anaconda channels.

    Args:
        channels:
            One of more upstream anaconda channels.
        local:
            The location of the local anaconda channel.
        subdirs:
            One or more anaconda subdirs.
        specs:
            One or more anaconda match specification strings

    Returns:
        A tuple of packages that should be added to the local anaconda channel,
        and packages that should be removed from the local anaconda channel.
    """
    local = Path(local)
    upstream = _ensure_list(upstream)
    subdirs = _ensure_subdirs(subdirs)
    specs = _ensure_list(specs)

    try:
        local_records = iterate(local.resolve().as_uri(), subdirs=subdirs)
    except UnavailableInvalidChannel:
        # TODO: check condition of local directory
        local_records = iter([])

    upstream_records = query(upstream, specs, subdirs=subdirs)
    removed, added = compare_records(local_records, upstream_records)

    return added, removed


def iterate(
    channels: OneOrMoreStrings, *, subdirs: Optional[OneOrMoreStrings] = None,
) -> Iterator[PackageRecord]:
    """Yields all the package records in a specified channels and subdirs.

    Args:
        channels: One of more upstream anaconda channels.
        subdirs: One or more anaconda subdirs (platforms).
    """
    channels = _ensure_list(channels)
    subdirs = _ensure_subdirs(subdirs)
    records = iter_channels(channels, subdirs)
    yield from records


def merge(
    local: PathOrString,
    patch: PathOrString,
    *,
    index: bool = True,
    progress: bool = False,
):
    """Merges a patch produced by conda_local with a local anaconda channel.

    Args:
        local: The location of the local anaconda channel.
        patch: The location of the conda_local patch directory.
        index: Determines if the local channel index should be updated.

    """
    patch = Path(patch)
    local = Path(local)
    f = sys.stdout if progress else None

    with Spinner("Copying patch", enabled=progress):
        for file in patch.glob("**/*"):
            if file.is_file():
                shutil.copy(file, local / file.relative_to(patch))
    print("Copying patch:", "done", file=f)

    if index:
        update_index(local, progress=progress, subdirs=[])
        print("Updating index:", "done", file=f)


def query(
    channels: OneOrMoreStrings,
    specs: OneOrMoreStrings,
    *,
    subdirs: Optional[OneOrMoreStrings] = None,
    graph_file: Optional[PathOrString] = None,
) -> Iterable[PackageRecord]:
    """Executes a query of anaconda match specifications against anaconda channels.

    Args:
        channels: One or more upstream anaconda channels.
        subdirs: One or more anaconda subdirs (platforms).
        specs: One or more anaconda match specification strings.
        graph_file: Optional save location of the query dependency graph.

    Returns:
        A iterable of resulting package records from the executed query.
    """
    channels = _ensure_list(channels)
    subdirs = _ensure_subdirs(subdirs)
    specs = _ensure_list(specs)
    finder = DependencyFinder(channels, subdirs)
    records, graph = finder.search(specs)
    if graph_file is not None:
        graph_file = Path(graph_file)
    return records


def sync(
    channels: OneOrMoreStrings,
    local: PathOrString,
    specs: OneOrMoreStrings,
    *,
    subdirs: Optional[OneOrMoreStrings] = None,
    index: bool = True,
    verify: bool = True,
    patch: PathOrString = "",
    progress: bool = False,
) -> None:
    """Syncs a local anaconda channel with upstream anaconda channels.

    Args:
        channels: One or more upstream anaconda channels.
        local: The location of the local anaconda channel.
        subdirs: One or more anaconda subdirs (platforms).
        specs: One or more anaconda match specification strings
        index: Determines if the local channel index should be updated.
        verify: Determines if downloaded packages should be verified.
        patch: The location of the patch folder.
        progress: Determines if a progress bar should be shown.
    """
    channels = _ensure_list(channels)
    local = _ensure_local_channel(local)
    subdirs = _ensure_subdirs(subdirs)
    f = sys.stdout if progress else None

    destination = local if not patch else Path(patch)
    destination.mkdir(parents=True, exist_ok=True)

    with Spinner("Reading upstream channels", enabled=progress):
        added_records, _ = diff(local, channels, specs, subdirs)

    for subdir in tqdm(
        subdirs,
        desc="Downloading patch instructions",
        disable=not progress,
        leave=False,
    ):
        download_patch_instructions(channels, destination, subdir)
    print("Downloading patches instructions:", "done", file=f)

    records = sorted(added_records, key=lambda rec: rec.fn)
    for record in tqdm(
        records, desc="Downloading packages", disable=not progress, leave=False
    ):
        download_package(record, destination, verify)
    print("Downloading packages:", "done", file=f)

    if index and not patch:
        update_index(local, progress=progress, subdirs=subdirs)
        print("Updating index:", "done", file=f)


def _ensure_list(items: Union[T, Iterable[T]]) -> List[T]:
    """Ensures that an input parameter is list of elements."""
    if not isinstance(items, Iterable):
        return cast(List[T], [items])
    if isinstance(items, str):
        return cast(List[T], [items])
    return cast(List[T], list(items))


def _ensure_local_channel(path: PathOrString) -> Path:
    """Ensures that a local path is a valid anaconda channel."""
    path = Path(path)
    noarch_repo = path / "noarch" / "repodata.json"
    noarch_repo.parent.mkdir(exist_ok=True, parents=True)
    noarch_repo.touch(exist_ok=True)
    return path


def _ensure_subdirs(subdirs: Optional[OneOrMoreStrings]) -> List[str]:
    """Ensures that an input parameter is list of subdirs."""
    if subdirs is None:
        return get_current_subdirs()
    return _ensure_list(subdirs)
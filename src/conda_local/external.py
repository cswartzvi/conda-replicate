"""External interface for the `conda` and `conda_build` public (and not so
public) APIs.

Notes:
    Within this module anaconda channels can be referened by identifier or uri, e.g.:
    * "conda-forge"
    * "https://repo.anaconda.com/pkgs/main"
    * "file:///path/to/local/channel"

    The anaconda match specifications are, fundamentally, a query language for conda
    packages.  Any of the fields that comprise a :class:`PackageRecord` can be used to
    compose a :class:`MatchSpec`. For more information see the docs:
    https://docs.conda.io/projects/conda/en/latest/user-guide/concepts/pkg-specs.html#package-match-specifications  # noqa

"""

import hashlib
import json
import logging
import tarfile
import tempfile
import urllib.parse
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Set, Tuple, Union

import conda.api
import conda.base.context
import conda.exports
import conda.models.channel
import conda_build.api
import requests

# Passthrough imports
from conda.common.io import Spinner  # noqa
from conda.exceptions import UnavailableInvalidChannel  # noqa
from conda.exports import MatchSpec, PackageRecord

from conda_local.grouping import Grouping, groupby

LOGGER = logging.Logger(__name__)
PATCH_INSTRUCTIONS = "patch_instructions.json"


def compare_records(
    left: Iterable[PackageRecord], right: Iterable[PackageRecord]
) -> Tuple[Set[PackageRecord], Set[PackageRecord]]:
    """Compares two iterables of package records by computing set-based differences.

    Packages are compared by considering their platform sub-directory, name, version,
    build and build number. Note, channel is *not* considered.

    Args:
        left:
            An iterable of package records.
        right:
            An iterable of package records

    Notes:
        By default, a package record's hash includes it's channel. Because we may be
        comparing package records from different channels, we cannot use the default
        hash method. Therefore, in this function we use an internal no_channel_key
        function to group the incoming package records.

    Returns:
        A tuple of sets containing set differences (left - right and right - left).
    """

    def no_channel_key(record):
        return (
            record.subdir,
            record.name,
            record.version,
            record.build,
            record.build_number,
        )

    left_group = groupby(left, no_channel_key)
    right_group = groupby(right, no_channel_key)
    only_in_left = {
        record
        for key in left_group.keys() - right_group.keys()
        for record in left_group[key]
    }
    only_in_right = {
        record
        for key in right_group.keys() - left_group.keys()
        for record in right_group[key]
    }
    return only_in_left, only_in_right


def create_spec_lookup(specs: Iterable[str]) -> Grouping[str, MatchSpec]:
    """Returns match specification objects grouped by associated package names.

    Args:
        specs:
            An interable of anaconda match specifications strings.
    """
    return groupby([MatchSpec(spec) for spec in specs], lambda spec: spec.name)


def download_package(
    record: PackageRecord, destination: Path, verify: bool = True
) -> None:
    """Downloads the anaconda package associated with a specified package record.

    Args:
        record:
            The package record associated with the anaconda package to be downloaded.
        destination:
            The directory where the package will be downloaded. Note that additional
            subdirs will be created within the destination directory if needed.
        verify:
            A flag indicating if packages should be verified.
    """
    sha256_hash = record.sha256 if verify else None
    size = record.size if verify else None
    path = destination / record.subdir / record.fn

    if path.exists():
        if verify:
            if verify_file(path, record):
                return  # skip file
            LOGGER.info(
                "Existing file failed verification, will be overwritten: %s",
                str(path.resolve()),
            )
        else:
            return  # skip file

    path.parent.mkdir(parents=True, exist_ok=True)
    conda.exports._download(record.url, path, sha256=sha256_hash, size=size)


def fetch_patch_instructions(
    channel: str,
    destination: Path,
    subdir: str,
    packages_to_remove: Optional[Iterable[PackageRecord]] = None,
) -> None:
    """Retrives patch instructions from a platform sub-directory in a anaconda channel.

    Args:
        channel:
            An anaconda channel url or identifier.
        subdirs:
            The platform sub-directories within the anaconda channel.
        destination:
            The directory where the package will be downloaded. Note that additional
            subdirs will be created within the destination directory if needed.
        packages_to_remove:
            Optional iterable of additional package records to remove. These packages
            will be added to the patch instructions.
    """

    base_url = conda.models.channel.all_channel_urls([channel], subdirs=[subdir])[0]
    url = urllib.parse.urljoin(base_url + "/", PATCH_INSTRUCTIONS)
    response = requests.get(url)
    response.raise_for_status()

    destination.mkdir(parents=True, exist_ok=True)
    instructions = destination / subdir / PATCH_INSTRUCTIONS
    instructions.parent.mkdir(parents=True, exist_ok=True)
    instructions.write_bytes(response.content)

    if packages_to_remove:
        with instructions.open("r+") as file:
            data = json.load(file)
            for package in packages_to_remove:
                data["remove"].append(package.fn)
            file.seek(0)
            json.dump(data, file)


def get_default_subdirs() -> List[str]:
    """Returns the default platform sub-directories for the current platform."""
    return list(conda.base.context.context.subdirs)


def iter_channel(
    channel: str, subdirs: Optional[Iterable[str]] = None
) -> Iterator[PackageRecord]:
    """Yields all package records from an anaconda channel.

    Args:
        channel:
            An anaconda channel url or identifier.
        subdirs:
            The platform sub-directories within the anaconda channel. If None, defaults
            to the standard subdirs for the current platform.
    """
    yield from query_channel(channel, ["*"], subdirs)


def query_channel(
    channel: str, specs: Iterable[str], subdirs: Optional[Iterable[str]] = None,
) -> Iterator[PackageRecord]:
    """Performs a package record query against the specified anaconda channels.

    Args:
        channel:
            An anaconda channel url or identifier.
        specs:
            The package match specifications used within the query
        subdirs:
            The platform sub-directories within the anaconda channel. If
            None defaults to the standard subdirs for the current platform.

    Yields:
        Package records resulting from the anaconda channel query.
    """
    yield from (
        result
        for spec in specs
        for result in conda.api.SubdirData.query_all(
            spec, channels=[channel], subdirs=subdirs
        )
    )


def setup_channel(path: Union[str, Path]) -> Path:
    """Setup the base requirements of local anaconda channels.

    No action is taken if the specified local anaconda chaneel already exists.

    Args:
        path: The current (or desired) location of the local anaconda channel.

    Returns:
        The path of the initialized local anaconda channel.
    """
    path = Path(path)
    noarch_repo = path / "noarch" / "repodata.json"
    noarch_repo.parent.mkdir(exist_ok=True, parents=True)
    noarch_repo.touch(exist_ok=True)
    return path


def update_index(
    target: Path, subdirs: Optional[Iterable[str]] = None, silent: bool = False,
):
    """Updates the channel index of a local anaconda channel.

    Args:
        target:
            The location of a local anaconda channel.
        subdirs:
            The platform sub-directories within the anaconda channel. If
            None defaults to the standard subdirs for the current platform.
        silent:
            A flag that indicates if the update should produce progress output. Note
            that this output is generated from within the wrapped ``update_index``
            and cannot currently be altered. Defaults to False.
    """
    if subdirs is None:
        subdirs = get_default_subdirs()
    patches = [Path(subdir) / PATCH_INSTRUCTIONS for subdir in subdirs]

    with tempfile.TemporaryDirectory() as tmpdir:
        # HACK: Currently, the patch_generator needs to be a tar.bz2 file
        # with patch instructions for individual platform in corresponding
        # sub-directories.
        patch_generator = None
        if patches:
            tmppath = Path(tmpdir)
            tarball = tmppath / "patch_generator.tar.bz2"
            with tarfile.open(tarball, "w:bz2") as tar:
                for patch in patches:
                    tar.add(target / patch, arcname=patch)
            patch_generator = str(tarball.resolve())  # must be string (or None)

        conda_build.api.update_index(
            target, patch_generator=patch_generator, progress=not silent
        )


def verify_file(path: Path, record: PackageRecord) -> bool:
    """Verifies that a file matches the sha256 and size of specified package record.

    Args:
        path:
            The location of a package records that is to be verified.
        record:
            The package record used to perfrom the verification.
    """
    file_size = path.stat().st_size
    file_bytes = path.read_bytes()
    file_hash = hashlib.sha256(file_bytes).hexdigest()
    return record.sha256 == file_hash and record.size == file_size

# -*- coding: utf-8 -*-

from __future__ import absolute_import, print_function

import collections
import copy
import hashlib
import os
import sys

from distutils.sysconfig import get_python_lib
from contextlib import contextmanager
from functools import partial

import attr
import pip_shims
import six
import vistir

from first import first
from cached_property import cached_property
from packaging.markers import Marker
from packaging.requirements import Requirement as PackagingRequirement
from packaging.specifiers import Specifier, SpecifierSet, LegacySpecifier, InvalidSpecifier
from packaging.utils import canonicalize_name
from six.moves.urllib import parse as urllib_parse
from six.moves.urllib.parse import unquote
from vistir.compat import Path, FileNotFoundError, lru_cache
from vistir.contextmanagers import temp_path
from vistir.misc import dedup
from vistir.path import (
    create_tracked_tempdir,
    get_converted_relative_path,
    is_file_url,
    is_valid_url,
    normalize_path,
    mkdir_p
)

from ..exceptions import RequirementError
from ..utils import (
    VCS_LIST,
    is_installable_file,
    is_vcs,
    add_ssh_scheme_to_git_uri,
    strip_ssh_from_git_uri,
    get_setup_paths
)
from .setup_info import SetupInfo, _prepare_wheel_building_kwargs
from .utils import (
    HASH_STRING,
    build_vcs_uri,
    extras_to_string,
    filter_none,
    format_requirement,
    get_version,
    init_requirement,
    is_pinned_requirement,
    make_install_requirement,
    parse_extras,
    specs_to_string,
    split_markers_from_line,
    split_ref_from_uri,
    split_vcs_method_from_uri,
    validate_path,
    validate_specifiers,
    validate_vcs,
    normalize_name,
    create_link,
    get_pyproject,
    convert_direct_url_to_url,
    URL_RE,
    DIRECT_URL_RE,
    get_default_pyproject_backend
)

from ..environment import MYPY_RUNNING

if MYPY_RUNNING:
    from typing import Optional, TypeVar, List, Dict, Union, Any, Tuple, Set, Text
    from pip_shims.shims import Link, InstallRequirement
    RequirementType = TypeVar('RequirementType', covariant=True, bound=PackagingRequirement)
    from six.moves.urllib.parse import SplitResult
    from .vcs import VCSRepository
    NON_STRING_ITERABLE = Union[List, Set, Tuple]


SPECIFIERS_BY_LENGTH = sorted(list(Specifier._operators.keys()), key=len, reverse=True)


run = partial(vistir.misc.run, combine_stderr=False, return_object=True, nospin=True)


class Line(object):
    def __init__(self, line, extras=None):
        # type: (Text, Optional[NON_STRING_ITERABLE]) -> None
        self.editable = False  # type: bool
        if line.startswith("-e "):
            line = line[len("-e "):]
            self.editable = True
        self.extras = ()  # type: Tuple[Text]
        if extras is not None:
            self.extras = tuple(sorted(set(extras)))
        self.line = line  # type: Text
        self.hashes = []  # type: List[Text]
        self.markers = None  # type: Optional[Text]
        self.vcs = None  # type: Optional[Text]
        self.path = None  # type: Optional[Text]
        self.relpath = None  # type: Optional[Text]
        self.uri = None  # type: Optional[Text]
        self._link = None  # type: Optional[Link]
        self.is_local = False  # type: bool
        self._name = None  # type: Optional[Text]
        self._specifier = None  # type: Optional[Text]
        self.parsed_marker = None  # type: Optional[Marker]
        self.preferred_scheme = None  # type: Optional[Text]
        self._requirement = None  # type: Optional[PackagingRequirement]
        self.is_direct_url = False  # type: bool
        self._parsed_url = None  # type: Optional[urllib_parse.ParseResult]
        self._setup_cfg = None  # type: Optional[Text]
        self._setup_py = None  # type: Optional[Text]
        self._pyproject_toml = None  # type: Optional[Text]
        self._pyproject_requires = None  # type: Optional[List[Text]]
        self._pyproject_backend = None  # type: Optional[Text]
        self._wheel_kwargs = None  # type: Dict[Text, Text]
        self._vcsrepo = None  # type: Optional[VCSRepository]
        self._setup_info = None  # type: Optional[SetupInfo]
        self._ref = None  # type: Optional[Text]
        self._ireq = None  # type: Optional[InstallRequirement]
        self._src_root = None  # type: Optional[Text]
        self.dist = None  # type: Any
        super(Line, self).__init__()
        self.parse()

    def __hash__(self):
        return hash((
            self.editable, self.line, self.markers, tuple(self.extras),
            tuple(self.hashes), self.vcs, self.ireq)
        )

    def __repr__(self):
        try:
            return (
                "<Line (editable={self.editable}, name={self._name}, path={self.path}, "
                "uri={self.uri}, extras={self.extras}, markers={self.markers}, vcs={self.vcs}"
                ", specifier={self._specifier}, pyproject={self._pyproject_toml}, "
                "pyproject_requires={self._pyproject_requires}, "
                "pyproject_backend={self._pyproject_backend}, ireq={self._ireq})>".format(
                    self=self
            ))
        except Exception:
            return "<Line {0}>".format(self.__dict__.values())

    @classmethod
    def split_hashes(cls, line):
        # type: (Text) -> Tuple[Text, List[Text]]
        if "--hash" not in line:
            return line,  []
        split_line = line.split()
        line_parts = []  # type: List[Text]
        hashes = []  # type: List[Text]
        for part in split_line:
            if part.startswith("--hash"):
                param, _, value = part.partition("=")
                hashes.append(value)
            else:
                line_parts.append(part)
        line = " ".join(line_parts)
        return line, hashes

    @property
    def line_with_prefix(self):
        # type: () -> Text
        line = self.line
        extras_str = extras_to_string(self.extras)
        if self.is_direct_url:
            line = self.link.url
            # if self.link.egg_info and self.extras:
            #     line = "{0}{1}".format(line, extras_str)
        elif extras_str:
            if self.is_vcs:
                line = self.link.url
                if "git+file:/" in line and "git+file:///" not in line:
                    line = line.replace("git+file:/", "git+file:///")
            else:
                line = "{0}{1}".format(line, extras_str)
        if self.editable:
            return "-e {0}".format(line)
        return line

    @property
    def line_for_ireq(self):
        # type: () -> Text
        line = ""
        if self.is_file or self.is_url and not self.is_vcs:
            scheme = self.preferred_scheme if self.preferred_scheme is not None else "uri"
            local_line = next(iter([
                os.path.dirname(os.path.abspath(f)) for f in [
                    self.setup_py, self.setup_cfg, self.pyproject_toml
                ] if f is not None
            ]), None)
            if local_line and self.extras:
                local_line = "{0}{1}".format(local_line, extras_to_string(self.extras))
            line = local_line if local_line is not None else self.line
            if scheme == "path":
                if not line and self.base_path is not None:
                    line = os.path.abspath(self.base_path)
            else:
                if DIRECT_URL_RE.match(self.line):
                    self._requirement = init_requirement(self.line)
                    line = convert_direct_url_to_url(self.line)
                else:
                    line = self.link.url

        if self.editable:
            if not line:
                if self.is_path or self.is_file:
                    if not self.path:
                        line = pip_shims.shims.url_to_path(self.url)
                    else:
                        line = self.path
                    if self.extras:
                        line = "{0}{1}".format(line, extras_to_string(self.extras))
                else:
                    line = self.link.url
        elif self.is_vcs and not self.editable:
            line = add_ssh_scheme_to_git_uri(self.line)
        if not line:
            line = self.line
        return line

    @property
    def base_path(self):
        # type: () -> Optional[Text]
        if not self.link and not self.path:
            self.parse_link()
        if not self.path:
            pass
        path = normalize_path(self.path)
        if os.path.exists(path) and os.path.isdir(path):
            path = path
        elif os.path.exists(path) and os.path.isfile(path):
            path = os.path.dirname(path)
        else:
            path = None
        return path

    @property
    def setup_py(self):
        # type: () -> Optional[Text]
        if self._setup_py is None:
            self.populate_setup_paths()
        return self._setup_py

    @property
    def setup_cfg(self):
        # type: () -> Optional[Text]
        if self._setup_cfg is None:
            self.populate_setup_paths()
        return self._setup_cfg

    @property
    def pyproject_toml(self):
        # type: () -> Optional[Text]
        if self._pyproject_toml is None:
            self.populate_setup_paths()
        return self._pyproject_toml

    @property
    def specifier(self):
        # type: () -> Optional[Text]
        options = [self._specifier]
        for req in (self.ireq, self.requirement):
            if req is not None and getattr(req, "specifier", None):
                options.append(req.specifier)
        specifier = next(iter(spec for spec in options if spec is not None), None)
        if specifier is not None:
            specifier = specs_to_string(specifier)
        elif specifier is None and not self.is_named and self._setup_info is not None:
            if self._setup_info.version:
                specifier = "=={0}".format(self._setup_info.version)
        if specifier:
            self._specifier = specifier
        return self._specifier

    @specifier.setter
    def specifier(self, spec):
        # type: (str) -> None
        if not spec.startswith("=="):
            spec = "=={0}".format(spec)
        self._specifier = spec
        self.specifiers = SpecifierSet(spec)

    @property
    def specifiers(self):
        # type: () -> Optional[SpecifierSet]
        ireq_needs_specifier = False
        req_needs_specifier = False
        if self.ireq is None or self.ireq.req is None or not self.ireq.req.specifier:
            ireq_needs_specifier = True
        if self.requirement is None or not self.requirement.specifier:
            req_needs_specifier = True
        if any([ireq_needs_specifier, req_needs_specifier]):
            # TODO: Should we include versions for VCS dependencies? IS there a reason not
            # to? For now we are using hashes as the equivalent to pin
            # note: we need versions for direct dependencies at the very least
            if self.is_file or self.is_url or self.is_path or (self.is_vcs and not self.editable):
                if self.specifier is not None:
                    specifier = self.specifier
                    if not isinstance(specifier, SpecifierSet):
                        specifier = SpecifierSet(specifier)
                    self.specifiers = specifier
                    return specifier
        if self.ireq is not None and self.ireq.req is not None:
            return self.ireq.req.specifier
        elif self.requirement is not None:
            return self.requirement.specifier
        return None

    @specifiers.setter
    def specifiers(self, specifiers):
        # type: (Union[Text, SpecifierSet]) -> None
        if not isinstance(specifiers, SpecifierSet):
            if isinstance(specifiers, six.string_types):
                specifiers = SpecifierSet(specifiers)
            else:
                raise TypeError("Must pass a string or a SpecifierSet")
        specs = self.get_requirement_specs(specifiers)
        if self.ireq is not None and self.ireq.req is not None:
            self._ireq.req.specifier = specifiers
            self._ireq.req.specs = specs
        if self.requirement is not None:
            self.requirement.specifier = specifiers
            self.requirement.specs = specs

    @classmethod
    def get_requirement_specs(cls, specifierset):
        # type: (SpecifierSet) -> List[Tuple[Text, Text]]
        specs = []
        spec = next(iter(specifierset._specs), None)
        if spec:
            specs.append(spec._spec)
        return specs

    @property
    def requirement(self):
        # type: () -> Optional[PackagingRequirement]
        if self._requirement is None:
            self.parse_requirement()
            if self._requirement is None and self._name is not None:
                self._requirement = init_requirement(canonicalize_name(self.name))
                if self.is_file or self.is_url and self._requirement is not None:
                    self._requirement.url = self.url
        if self._requirement and self._requirement.specifier and not self._requirement.specs:
            specs = self.get_requirement_specs(self._requirement.specifier)
            self._requirement.specs = specs
        return self._requirement

    def populate_setup_paths(self):
        # type: () -> None
        if not self.link and not self.path:
            self.parse_link()
        if not self.path:
            return
        base_path = self.base_path
        if base_path is None:
            return
        setup_paths = get_setup_paths(self.base_path, subdirectory=self.subdirectory)  # type: Dict[Text, Optional[Text]]
        self._setup_py = setup_paths.get("setup_py")
        self._setup_cfg = setup_paths.get("setup_cfg")
        self._pyproject_toml = setup_paths.get("pyproject_toml")

    @property
    def pyproject_requires(self):
        # type: () -> Optional[List[Text]]
        if self._pyproject_requires is None and self.pyproject_toml is not None:
            pyproject_requires, pyproject_backend = get_pyproject(self.path)
            self._pyproject_requires = pyproject_requires
            self._pyproject_backend = pyproject_backend
        return self._pyproject_requires

    @property
    def pyproject_backend(self):
        # type: () -> Optional[Text]
        if self._pyproject_requires is None and self.pyproject_toml is not None:
            pyproject_requires, pyproject_backend = get_pyproject(self.path)
            if not pyproject_backend and self.setup_cfg is not None:
                setup_dict = SetupInfo.get_setup_cfg(self.setup_cfg)
                pyproject_backend = get_default_pyproject_backend()
                pyproject_requires = setup_dict.get("build_requires", ["setuptools", "wheel"])

            self._pyproject_requires = pyproject_requires
            self._pyproject_backend = pyproject_backend
        return self._pyproject_backend

    def parse_hashes(self):
        # type: () -> None
        """
        Parse hashes from *self.line* and set them on the current object.
        :returns: Nothing
        :rtype: None
        """

        line, hashes = self.split_hashes(self.line)
        self.hashes = hashes
        self.line = line

    def parse_extras(self):
        # type: () -> None
        """
        Parse extras from *self.line* and set them on the current object
        :returns: Nothing
        :rtype: None
        """

        extras = None
        if "@" in self.line or self.is_vcs or self.is_url:
            line = "{0}".format(self.line)
            match = DIRECT_URL_RE.match(line)
            if match is None:
                match = URL_RE.match(line)
            else:
                self.is_direct_url = True
            if match is not None:
                match_dict = match.groupdict()
                name = match_dict.get("name")
                extras = match_dict.get("extras")
                scheme = match_dict.get("scheme")
                host = match_dict.get("host")
                path = match_dict.get("path")
                ref = match_dict.get("ref")
                subdir = match_dict.get("subdirectory")
                pathsep = match_dict.get("pathsep", "/")
                url = scheme
                if host:
                    url = "{0}{1}".format(url, host)
                if path:
                    url = "{0}{1}{2}".format(url, pathsep, path)
                    if self.is_vcs and ref:
                        url = "{0}@{1}".format(url, ref)
                    if name:
                        url = "{0}#egg={1}".format(url, name)
                        if extras:
                            url = "{0}{1}".format(url, extras)
                    elif is_file_url(url) and extras and not name and self.editable:
                        url = "{0}{1}{2}".format(pathsep, path, extras)
                    if subdir:
                        url = "{0}&subdirectory={1}".format(url, subdir)
                elif extras and not path:
                    url = "{0}{1}".format(url, extras)
                self.line = add_ssh_scheme_to_git_uri(url)
                if name:
                    self._name = name
            # line = add_ssh_scheme_to_git_uri(self.line)
            # parsed = urllib_parse.urlparse(line)
            # if not parsed.scheme and "@" in line:
            #     matched = URL_RE.match(line)
            #     if matched is None:
            #         matched = NAME_RE.match(line)
            #     if matched:
            #         name = matched.groupdict().get("name")
            #     if name is not None:
            #         self._name = name
            #         extras = matched.groupdict().get("extras")
            #     else:
            #         name, _, line = self.line.partition("@")
            #         name = name.strip()
            #         line = line.strip()
            #         matched = NAME_RE.match(name)
            #         match_dict = matched.groupdict()
            #         name = match_dict.get("name")
            #         extras = match_dict.get("extras")
            #         if is_vcs(line) or is_valid_url(line):
            #             self.is_direct_url = True
            #         # name, extras = pip_shims.shims._strip_extras(name)
            #     self._name = name
            #     self.line = line
            else:
                self.line, extras = pip_shims.shims._strip_extras(self.line)
        else:
            self.line, extras = pip_shims.shims._strip_extras(self.line)
        if extras is not None:
            extras = set(parse_extras(extras))
        if self._name:
            self._name, name_extras = pip_shims.shims._strip_extras(self._name)
            if name_extras:
                name_extras = set(parse_extras(name_extras))
                if extras:
                    extras |= name_extras
                else:
                    extras = name_extras
        if extras is not None:
            self.extras = tuple(sorted(extras))

    def get_url(self):
        # type: () -> Text
        """Sets ``self.name`` if given a **PEP-508** style URL"""

        line = self.line
        if self.vcs is not None and self.line.startswith("{0}+".format(self.vcs)):
            _, _, _parseable = self.line.partition("+")
            parsed = urllib_parse.urlparse(add_ssh_scheme_to_git_uri(_parseable))
            line, _ = split_ref_from_uri(line)
        else:
            parsed = urllib_parse.urlparse(add_ssh_scheme_to_git_uri(line))
        if "@" in self.line and parsed.scheme == "":
            name, _, url = self.line.partition("@")
            if self._name is None:
                url = url.strip()
                self._name = name.strip()
                if is_valid_url(url):
                    self.is_direct_url = True
            line = url.strip()
            parsed = urllib_parse.urlparse(line)
            url_path = parsed.path
            if "@" in url_path:
                url_path, _, _ = url_path.rpartition("@")
            parsed = parsed._replace(path=url_path)
        self._parsed_url = parsed
        return line

    @property
    def name(self):
        # type: () -> Optional[Text]
        if self._name is None:
            self.parse_name()
            if self._name is None and not self.is_named and not self.is_wheel:
                if self.setup_info:
                    self._name = self.setup_info.name
        return self._name

    @name.setter
    def name(self, name):
        # type: (Text) -> None
        self._name = name
        if self._setup_info:
            self._setup_info.name = name
        if self.requirement:
            self._requirement.name = name
        if self.ireq and self.ireq.req:
            self._ireq.req.name = name

    @property
    def url(self):
        # type: () -> Optional[Text]
        if self.uri is not None:
            url = add_ssh_scheme_to_git_uri(self.uri)
        else:
            url = getattr(self.link, "url_without_fragment", None)
        if url is not None:
            url = add_ssh_scheme_to_git_uri(unquote(url))
        if url is not None and self._parsed_url is None:
            if self.vcs is not None:
                _, _, _parseable = url.partition("+")
            self._parsed_url = urllib_parse.urlparse(_parseable)
        if self.is_vcs:
            # strip the ref from the url
            url, _ = split_ref_from_uri(url)
        return url

    @property
    def link(self):
        # type: () -> Link
        if self._link is None:
            self.parse_link()
        return self._link

    @property
    def subdirectory(self):
        # type: () -> Optional[Text]
        if self.link is not None:
            return self.link.subdirectory_fragment
        return ""

    @property
    def is_wheel(self):
        # type: () -> bool
        if self.link is None:
            return False
        return self.link.is_wheel

    @property
    def is_artifact(self):
        # type: () -> bool
        if self.link is None:
            return False
        return self.link.is_artifact

    @property
    def is_vcs(self):
        # type: () -> bool
        # Installable local files and installable non-vcs urls are handled
        # as files, generally speaking
        if is_vcs(self.line) or is_vcs(self.get_url()):
            return True
        return False

    @property
    def is_url(self):
        # type: () -> bool
        url = self.get_url()
        if (is_valid_url(url) or is_file_url(url)):
            return True
        return False

    @property
    def is_path(self):
        # type: () -> bool
        if self.path and (
            self.path.startswith(".") or os.path.isabs(self.path) or
            os.path.exists(self.path)
        ) and is_installable_file(self.path):
            return True
        elif (os.path.exists(self.line) and is_installable_file(self.line)) or (
            os.path.exists(self.get_url()) and is_installable_file(self.get_url())
        ):
            return True
        return False

    @property
    def is_file_url(self):
        # type: () -> bool
        url = self.get_url()
        parsed_url_scheme = self._parsed_url.scheme if self._parsed_url else ""
        if url and is_file_url(self.get_url()) or parsed_url_scheme == "file":
            return True
        return False

    @property
    def is_file(self):
        # type: () -> bool
        if self.is_path or (
            is_file_url(self.get_url()) and is_installable_file(self.get_url())
        ) or (
            self._parsed_url and self._parsed_url.scheme == "file" and
            is_installable_file(urllib_parse.urlunparse(self._parsed_url))
        ):
            return True
        return False

    @property
    def is_named(self):
        # type: () -> bool
        return not (self.is_file_url or self.is_url or self.is_file or self.is_vcs)

    @property
    def ref(self):
        # type: () -> Optional[Text]
        if self._ref is None and self.relpath is not None:
            self.relpath, self._ref = split_ref_from_uri(self.relpath)
        return self._ref

    @property
    def ireq(self):
        # type: () -> Optional[pip_shims.InstallRequirement]
        if self._ireq is None:
            self.parse_ireq()
        return self._ireq

    @property
    def is_installable(self):
        # type: () -> bool
        possible_paths = (self.line, self.get_url(), self.path, self.base_path)
        return any(is_installable_file(p) for p in possible_paths if p is not None)

    @property
    def wheel_kwargs(self):
        if not self._wheel_kwargs:
            self._wheel_kwargs = _prepare_wheel_building_kwargs(self.ireq)
        return self._wheel_kwargs

    def get_setup_info(self):
        # type: () -> SetupInfo
        setup_info = SetupInfo.from_ireq(self.ireq)
        if not setup_info.name:
            setup_info.get_info()
        return setup_info

    @property
    def setup_info(self):
        # type: () -> Optional[SetupInfo]
        if self._setup_info is None and not self.is_named and not self.is_wheel:
            if self._setup_info:
                if not self._setup_info.name:
                    self._setup_info.get_info()
            else:
                # make two attempts at this before failing to allow for stale data
                try:
                    self.setup_info = self.get_setup_info()
                except FileNotFoundError:
                    try:
                        self.setup_info = self.get_setup_info()
                    except FileNotFoundError:
                        raise
        return self._setup_info

    @setup_info.setter
    def setup_info(self, setup_info):
        # type: (SetupInfo) -> None
        self._setup_info = setup_info
        if setup_info.version:
            self.specifier = setup_info.version
        if setup_info.name and not self.name:
            self.name = setup_info.name

    def _get_vcsrepo(self):
        # type: () -> Optional[VCSRepository]
        from .vcs import VCSRepository
        checkout_directory = self.wheel_kwargs["src_dir"]  # type: ignore
        if self.name is not None:
            checkout_directory = os.path.join(checkout_directory, self.name)  # type: ignore
        vcsrepo = VCSRepository(
            url=self.link.url,
            name=self.name,
            ref=self.ref if self.ref else None,
            checkout_directory=checkout_directory,
            vcs_type=self.vcs,
            subdirectory=self.subdirectory,
        )
        if not (
            self.link.scheme.startswith("file") and
            self.editable
        ):
            vcsrepo.obtain()
        return vcsrepo

    @property
    def vcsrepo(self):
        # type: () -> Optional[VCSRepository]
        if self._vcsrepo is None and self.is_vcs:
            self._vcsrepo = self._get_vcsrepo()
        return self._vcsrepo

    @vcsrepo.setter
    def vcsrepo(self, repo):
        # type (VCSRepository) -> None
        self._vcsrepo = repo
        ireq = self.ireq
        wheel_kwargs = self.wheel_kwargs.copy()
        wheel_kwargs["src_dir"] = repo.checkout_directory
        ireq.source_dir = wheel_kwargs["src_dir"]
        ireq.build_location(wheel_kwargs["build_dir"])
        ireq._temp_build_dir.path = wheel_kwargs["build_dir"]
        with temp_path():
            sys.path = [repo.checkout_directory, "", ".", get_python_lib(plat_specific=0)]
            setupinfo = SetupInfo.create(
                repo.checkout_directory, ireq=ireq, subdirectory=self.subdirectory,
                kwargs=wheel_kwargs
            )
            self._setup_info = setupinfo
            self._setup_info.reload()

    def get_ireq(self):
        # type: () -> InstallRequirement
        line = self.line_for_ireq
        if self.editable:
            ireq = pip_shims.shims.install_req_from_editable(line)
        else:
            ireq = pip_shims.shims.install_req_from_line(line)
        if self.is_named:
            ireq = pip_shims.shims.install_req_from_line(self.line)
        if self.is_file or self.is_url:
            ireq.link = self.link
        if self.extras and not ireq.extras:
            ireq.extras = set(self.extras)
        if self.parsed_marker is not None and not ireq.markers:
            ireq.markers = self.parsed_marker
        if not ireq.req and self._requirement is not None:
            ireq.req = copy.deepcopy(self._requirement)
        return ireq

    def parse_ireq(self):
        # type: () -> None
        if self._ireq is None:
            self._ireq = self.get_ireq()
        if self._ireq is not None:
            if self.requirement is not None and self._ireq.req is None:
                self._ireq.req = self.requirement

    def _parse_wheel(self):
        # type: () -> Optional[Text]
        if not self.is_wheel:
            pass
        from pip_shims.shims import Wheel
        _wheel = Wheel(self.link.filename)
        name = _wheel.name
        version = _wheel.version
        self._specifier = "=={0}".format(version)
        return name

    def _parse_name_from_link(self):
        # type: () -> Optional[Text]

        if self.link is None:
            return None
        if getattr(self.link, "egg_fragment", None):
            return self.link.egg_fragment
        elif self.is_wheel:
            return self._parse_wheel()
        return None

    def _parse_name_from_line(self):
        # type: () -> Optional[Text]

        if not self.is_named:
            pass
        try:
            self._requirement = init_requirement(self.line)
        except Exception:
            raise RequirementError("Failed parsing requirement from {0!r}".format(self.line))
        name = self._requirement.name
        if not self._specifier and self._requirement and self._requirement.specifier:
            self._specifier = specs_to_string(self._requirement.specifier)
        if self._requirement.extras and not self.extras:
            self.extras = self._requirement.extras
        if not name:
            name = self.line
            specifier_match = next(
                iter(spec for spec in SPECIFIERS_BY_LENGTH if spec in self.line), None
            )
            if specifier_match is not None:
                name, specifier_match, version = name.partition(specifier_match)
                self._specifier = "{0}{1}".format(specifier_match, version)
        return name

    def parse_name(self):
        # type: () -> None
        if self._name is None:
            name = None
            if self.link is not None:
                name = self._parse_name_from_link()
            if name is None and (
                (self.is_url or self.is_artifact or self.is_vcs) and self._parsed_url
            ):
                if self._parsed_url.fragment:
                    _, _, name = self._parsed_url.fragment.partition("egg=")
                    if "&" in name:
                        # subdirectory fragments might also be in here
                        name, _, _ = name.partition("&")
            if self.is_named:
                name = self._parse_name_from_line()
            if name is not None:
                name, extras = pip_shims.shims._strip_extras(name)
                if extras is not None and not self.extras:
                    self.extras = tuple(sorted(set(parse_extras(extras))))
                self._name = name

    def _parse_requirement_from_vcs(self):
        # type: () -> Optional[PackagingRequirement]
        if (
            self.uri != unquote(self.url)
            and "git+ssh://" in self.url
            and (self.uri is not None and "git+git@" in self.uri)
        ):
            self._requirement.line = self.uri
            self._requirement.url = self.url
            self._requirement.link = create_link(build_vcs_uri(
                vcs=self.vcs,
                uri=self.url,
                ref=self.ref,
                subdirectory=self.subdirectory,
                extras=self.extras,
                name=self.name
            ))
        # else:
        #     req.link = self.link
        if self.ref:
            if self._vcsrepo is not None:
                self._requirement.revision = self._vcsrepo.get_commit_hash()
            else:
                self._requirement.revision = self.ref
        return self._requirement

    def parse_requirement(self):
        # type: () -> None
        if self._name is None:
            self.parse_name()
            if not self._name and not self.is_vcs and not self.is_named:
                if self.setup_info and self.setup_info.name:
                    self._name = self.setup_info.name
        name, extras, url = self.requirement_info
        if name:
            self._requirement = init_requirement(name)  # type: PackagingRequirement
            if extras:
                self._requirement.extras = set(extras)
            if url:
                self._requirement.url = url
            if self.is_direct_url:
                url = self.link.url
            if self.link:
                self._requirement.link = self.link
            self._requirement.editable = self.editable
            if self.path and self.link and self.link.scheme.startswith("file"):
                self._requirement.local_file = True
                self._requirement.path = self.path
            if self.is_vcs:
                self._requirement.vcs = self.vcs
                self._requirement.line = self.link.url
                self._parse_requirement_from_vcs()
            else:
                self._requirement.line = self.line
            if self.parsed_marker is not None:
                self._requirement.marker = self.parsed_marker
            if self.specifiers:
                self._requirement.specifier = self.specifiers
                specs = []
                spec = next(iter(s for s in self.specifiers._specs), None)
                if spec:
                    specs.append(spec._spec)
                self._requirement.spec = spec
        else:
            if self.is_vcs:
                raise ValueError(
                    "pipenv requires an #egg fragment for version controlled "
                    "dependencies. Please install remote dependency "
                    "in the form {0}#egg=<package-name>.".format(url)
                )

    def parse_link(self):
        # type: () -> None
        if self.is_file or self.is_url or self.is_vcs:
            vcs, prefer, relpath, path, uri, link = FileRequirement.get_link_from_line(self.line)
            ref = None
            if link is not None and "@" in unquote(link.path) and uri is not None:
                uri, _, ref = unquote(uri).rpartition("@")
            if relpath is not None and "@" in relpath:
                relpath, _, ref = relpath.rpartition("@")
            if path is not None and "@" in path:
                path, _ = split_ref_from_uri(path)
            link_url = link.url_without_fragment
            if "@" in link_url:
                link_url, _ = split_ref_from_uri(link_url)
            self._ref = ref
            self.vcs = vcs
            self.preferred_scheme = prefer
            self.relpath = relpath
            self.path = path
            self.uri = uri
            if prefer in ("path", "relpath") or uri.startswith("file"):
                self.is_local = True
            if link.egg_fragment:
                name, extras = pip_shims.shims._strip_extras(link.egg_fragment)
                self.extras = tuple(sorted(set(parse_extras(extras))))
                self._name = name
            else:
                # set this so we can call `self.name` without a recursion error
                self._link = link
            if (self.is_direct_url or vcs) and self.name is not None and vcs is not None:
                self._link = create_link(
                    build_vcs_uri(vcs=vcs, uri=link_url, ref=ref,
                                  extras=self.extras, name=self.name,
                                  subdirectory=link.subdirectory_fragment
                                )
                )
            else:
                self._link = link

    def parse_markers(self):
        # type: () -> None
        if self.markers:
            markers = PackagingRequirement("fakepkg; {0}".format(self.markers)).marker
            self.parsed_marker = markers

    @property
    def requirement_info(self):
        # type: () -> Tuple(Optional[Text], Tuple[Optional[Text]], Optional[Text])
        """
        Generates a 3-tuple of the requisite *name*, *extras* and *url* to generate a
        :class:`~packaging.requirements.Requirement` out of.

        :return: A Tuple containing an optional name, a Tuple of extras names, and an optional URL.
        :rtype: Tuple[Optional[Text], Tuple[Optional[Text]], Optional[Text]]
        """

        # Direct URLs can be converted to packaging requirements directly, but
        # only if they are `file://` (with only two slashes)
        name = None
        extras = ()
        url = None
        # if self.is_direct_url:
        if self._name:
            name = canonicalize_name(self._name)
        if self.is_file or self.is_url or self.is_path or self.is_file_url or self.is_vcs:
            url = ""
            if self.is_vcs:
                url = self.url if self.url else self.uri
                if self.is_direct_url:
                    url = self.link.url_without_fragment
            else:
                if self.link:
                    url = self.link.url_without_fragment
                elif self.url:
                    url = self.url
                    if self.ref:
                        url = "{0}@{1}".format(url, self.ref)
                else:
                    url = self.uri
            if self.link and name is None:
                self._name = self.link.egg_fragment
                if self._name:
                    name = canonicalize_name(self._name)
            # return "{0}{1}@ {2}".format(
            #     normalize_name(self.name), extras_to_string(self.extras), url
            # )
        return (name, extras, url)

    @property
    def line_is_installable(self):
        # type: () -> bool
        """
        This is a safeguard against decoy requirements when a user installs a package
        whose name coincides with the name of a folder in the cwd, e.g. install *alembic*
        when there is a folder called *alembic* in the working directory.

        In this case we first need to check that the given requirement is a valid
        URL, VCS requirement, or installable filesystem path before deciding to treat it as
        a file requirement over a named requirement.
        """
        line = self.line
        if is_file_url(line):
            link = create_link(line)
            line = link.url_without_fragment
            line, _ = split_ref_from_uri(line)
        if (is_vcs(line) or (is_valid_url(line) and (
                    not is_file_url(line) or is_installable_file(line)))
                or is_installable_file(line)):
            return True
        return False

    def parse(self):
        # type: () -> None
        self.parse_hashes()
        self.line, self.markers = split_markers_from_line(self.line)
        self.parse_extras()
        self.line = self.line.strip('"').strip("'").strip()
        if self.line.startswith("git+file:/") and not self.line.startswith("git+file:///"):
            self.line = self.line.replace("git+file:/", "git+file:///")
        self.parse_markers()
        if self.is_file_url:
            if self.line_is_installable:
                self.populate_setup_paths()
            else:
                raise RequirementError(
                    "Supplied requirement is not installable: {0!r}".format(self.line)
                )
        self.parse_link()
        # self.parse_requirement()
        # self.parse_ireq()


@attr.s(slots=True, hash=True)
class NamedRequirement(object):
    name = attr.ib()  # type: Text
    version = attr.ib()  # type: Optional[Text]
    req = attr.ib()  # type: PackagingRequirement
    extras = attr.ib(default=attr.Factory(list))  # type: Tuple[Text]
    editable = attr.ib(default=False)  # type: bool
    _parsed_line = attr.ib(default=None)  # type: Optional[Line]

    @req.default
    def get_requirement(self):
        # type: () -> RequirementType
        req = init_requirement(
            "{0}{1}".format(canonicalize_name(self.name), self.version)
        )
        return req

    @property
    def parsed_line(self):
        # type: () -> Optional[Line]
        if self._parsed_line is None:
            self._parsed_line = Line(self.line_part)
        return self._parsed_line

    @classmethod
    def from_line(cls, line, parsed_line=None):
        # type: (Text, Optional[Line]) -> NamedRequirement
        req = init_requirement(line)
        specifiers = None  # type: Optional[Text]
        if req.specifier:
            specifiers = specs_to_string(req.specifier)
        req.line = line
        name = getattr(req, "name", None)
        if not name:
            name = getattr(req, "project_name", None)
            req.name = name
        if not name:
            name = getattr(req, "key", line)
            req.name = name
        creation_kwargs = {
            "name": name,
            "version": specifiers,
            "req": req,
            "parsed_line": parsed_line,
            "extras": None
        }
        extras = None  # type: Optional[Tuple[Text]]
        if req.extras:
            extras = list(req.extras)
        creation_kwargs["extras"] = extras
        return cls(**creation_kwargs)

    @classmethod
    def from_pipfile(cls, name, pipfile):
        # type: (Text, Dict[Text, Union[Text, Optional[Text], Optional[List[Text]]]]) -> NamedRequirement
        creation_args = {}  # type: Dict[Text, Union[Optional[Text], Optional[List[Text]]]]
        if hasattr(pipfile, "keys"):
            attr_fields = [field.name for field in attr.fields(cls)]
            creation_args = {k: v for k, v in pipfile.items() if k in attr_fields}
        creation_args["name"] = name
        version = get_version(pipfile)  # type: Optional[Text]
        extras = creation_args.get("extras", None)
        creation_args["version"] = version
        req = init_requirement("{0}{1}".format(name, version))
        if extras:
            req.extras += tuple(extras)
        creation_args["req"] = req
        return cls(**creation_args)  # type: ignore

    @property
    def line_part(self):
        # type: () -> Text
        # FIXME: This should actually be canonicalized but for now we have to
        # simply lowercase it and replace underscores, since full canonicalization
        # also replaces dots and that doesn't actually work when querying the index
        return normalize_name(self.name)

    @property
    def pipfile_part(self):
        # type: () -> Dict[Text, Any]
        pipfile_dict = attr.asdict(self, filter=filter_none).copy()  # type: ignore
        if "version" not in pipfile_dict:
            pipfile_dict["version"] = "*"
        if "_parsed_line" in pipfile_dict:
            pipfile_dict.pop("_parsed_line")
        name = pipfile_dict.pop("name")
        return {name: pipfile_dict}


LinkInfo = collections.namedtuple(
    "LinkInfo", ["vcs_type", "prefer", "relpath", "path", "uri", "link"]
)


@attr.s(slots=True, cmp=True, hash=True)
class FileRequirement(object):
    """File requirements for tar.gz installable files or wheels or setup.py
    containing directories."""

    #: Path to the relevant `setup.py` location
    setup_path = attr.ib(default=None, cmp=True)  # type: Optional[Text]
    #: path to hit - without any of the VCS prefixes (like git+ / http+ / etc)
    path = attr.ib(default=None, cmp=True)  # type: Optional[Text]
    #: Whether the package is editable
    editable = attr.ib(default=False, cmp=True)  # type: bool
    #: Extras if applicable
    extras = attr.ib(default=attr.Factory(tuple), cmp=True)  # type: Tuple[Text]
    _uri_scheme = attr.ib(default=None, cmp=True)  # type: Optional[Text]
    #: URI of the package
    uri = attr.ib(cmp=True)  # type: Optional[Text]
    #: Link object representing the package to clone
    link = attr.ib(cmp=True)  # type: Optional[Link]
    #: PyProject Requirements
    pyproject_requires = attr.ib(default=attr.Factory(tuple), cmp=True)  # type: Tuple
    #: PyProject Build System
    pyproject_backend = attr.ib(default=None, cmp=True)  # type: Optional[Text]
    #: PyProject Path
    pyproject_path = attr.ib(default=None, cmp=True)  # type: Optional[Text]
    #: Setup metadata e.g. dependencies
    _setup_info = attr.ib(default=None, cmp=True)  # type: Optional[SetupInfo]
    _has_hashed_name = attr.ib(default=False, cmp=True)  # type: bool
    _parsed_line = attr.ib(default=None, cmp=False, hash=True)  # type: Optional[Line]
    #: Package name
    name = attr.ib(cmp=True)  # type: Optional[Text]
    #: A :class:`~pkg_resources.Requirement` isntance
    req = attr.ib(cmp=True)  # type: Optional[PackagingRequirement]

    @classmethod
    def get_link_from_line(cls, line):
        # type: (Text) -> LinkInfo
        """Parse link information from given requirement line.

        Return a 6-tuple:

        - `vcs_type` indicates the VCS to use (e.g. "git"), or None.
        - `prefer` is either "file", "path" or "uri", indicating how the
            information should be used in later stages.
        - `relpath` is the relative path to use when recording the dependency,
            instead of the absolute path/URI used to perform installation.
            This can be None (to prefer the absolute path or URI).
        - `path` is the absolute file path to the package. This will always use
            forward slashes. Can be None if the line is a remote URI.
        - `uri` is the absolute URI to the package. Can be None if the line is
            not a URI.
        - `link` is an instance of :class:`pip._internal.index.Link`,
            representing a URI parse result based on the value of `uri`.

        This function is provided to deal with edge cases concerning URIs
        without a valid netloc. Those URIs are problematic to a straight
        ``urlsplit` call because they cannot be reliably reconstructed with
        ``urlunsplit`` due to a bug in the standard library:

        >>> from urllib.parse import urlsplit, urlunsplit
        >>> urlunsplit(urlsplit('git+file:///this/breaks'))
        'git+file:/this/breaks'
        >>> urlunsplit(urlsplit('file:///this/works'))
        'file:///this/works'

        See `https://bugs.python.org/issue23505#msg277350`.
        """

        # Git allows `git@github.com...` lines that are not really URIs.
        # Add "ssh://" so we can parse correctly, and restore afterwards.
        fixed_line = add_ssh_scheme_to_git_uri(line)  # type: Text
        added_ssh_scheme = fixed_line != line  # type: bool

        # We can assume a lot of things if this is a local filesystem path.
        if "://" not in fixed_line:
            p = Path(fixed_line).absolute()  # type: Path
            path = p.as_posix()  # type: Optional[Text]
            uri = p.as_uri()  # type: Text
            link = create_link(uri)  # type: Link
            relpath = None  # type: Optional[Text]
            try:
                relpath = get_converted_relative_path(path)
            except ValueError:
                relpath = None
            return LinkInfo(None, "path", relpath, path, uri, link)

        # This is an URI. We'll need to perform some elaborated parsing.

        parsed_url = urllib_parse.urlsplit(fixed_line)  # type: SplitResult
        original_url = parsed_url._replace()  # type: SplitResult

        # Split the VCS part out if needed.
        original_scheme = parsed_url.scheme  # type: Text
        vcs_type = None  # type: Optional[Text]
        if "+" in original_scheme:
            scheme = None  # type: Optional[Text]
            vcs_type, _, scheme = original_scheme.partition("+")
            parsed_url = parsed_url._replace(scheme=scheme)
            prefer = "uri"  # type: Text
        else:
            vcs_type = None
            prefer = "file"

        if parsed_url.scheme == "file" and parsed_url.path:
            # This is a "file://" URI. Use url_to_path and path_to_url to
            # ensure the path is absolute. Also we need to build relpath.
            path = Path(
                pip_shims.shims.url_to_path(urllib_parse.urlunsplit(parsed_url))
            ).as_posix()
            try:
                relpath = get_converted_relative_path(path)
            except ValueError:
                relpath = None
            uri = pip_shims.shims.path_to_url(path)
        else:
            # This is a remote URI. Simply use it.
            path = None
            relpath = None
            # Cut the fragment, but otherwise this is fixed_line.
            uri = urllib_parse.urlunsplit(
                parsed_url._replace(scheme=original_scheme, fragment="")
            )

        if added_ssh_scheme:
            original_uri = urllib_parse.urlunsplit(
                original_url._replace(scheme=original_scheme, fragment="")
            )
            uri = strip_ssh_from_git_uri(original_uri)

        # Re-attach VCS prefix to build a Link.
        link = create_link(
            urllib_parse.urlunsplit(parsed_url._replace(scheme=original_scheme))
        )

        return LinkInfo(vcs_type, prefer, relpath, path, uri, link)

    @property
    def setup_py_dir(self):
        # type: () -> Optional[Text]
        if self.setup_path:
            return os.path.dirname(os.path.abspath(self.setup_path))
        return None

    @property
    def dependencies(self):
        # type: () -> Tuple[Dict[Text, PackagingRequirement], List[Union[Text, PackagingRequirement]], List[Text]]
        build_deps = []  # type: List[Union[Text, PackagingRequirement]]
        setup_deps = []  # type: List[Text]
        deps = {}  # type: Dict[Text, PackagingRequirement]
        if self.setup_info:
            setup_info = self.setup_info.as_dict()
            deps.update(setup_info.get("requires", {}))
            setup_deps.extend(setup_info.get("setup_requires", []))
            build_deps.extend(setup_info.get("build_requires", []))
        if self.pyproject_requires:
            build_deps.extend(list(self.pyproject_requires))
        setup_deps = list(set(setup_deps))
        build_deps = list(set(build_deps))
        return deps, setup_deps, build_deps

    def __attrs_post_init__(self):
        # type: () -> None
        if self.name is None and self.parsed_line:
            if self.parsed_line.setup_info:
                self._setup_info = self.parsed_line.setup_info
                if self.parsed_line.setup_info.name:
                    self.name = self.parsed_line.setup_info.name
        if self.req is None and self._parsed_line.requirement is not None:
            self.req = self._parsed_line.requirement
        if self._parsed_line and self._parsed_line.ireq and not self._parsed_line.ireq.req:
            if self.req is not None:
                self._parsed_line._ireq.req = self.req

    @property
    def setup_info(self):
        # type: () -> SetupInfo
        from .setup_info import SetupInfo
        if self._setup_info is None and self.parsed_line:
            if self.parsed_line.setup_info:
                if not self._parsed_line.setup_info.name:
                    self._parsed_line._setup_info.get_info()
                self._setup_info = self.parsed_line.setup_info
            elif self.parsed_line.ireq and not self.parsed_line.is_wheel:
                self._setup_info = SetupInfo.from_ireq(self.parsed_line.ireq)
            else:
                if self.link and not self.link.is_wheel:
                    self._setup_info = Line(self.line_part).setup_info
                    self._setup_info.get_info()
        return self._setup_info

    @setup_info.setter
    def setup_info(self, setup_info):
        # type: (SetupInfo) -> None
        self._setup_info = setup_info
        if self._parsed_line:
            self._parsed_line._setup_info = setup_info

    @uri.default
    def get_uri(self):
        # type: () -> Text
        if self.path and not self.uri:
            self._uri_scheme = "path"
            return pip_shims.shims.path_to_url(os.path.abspath(self.path))
        elif getattr(self, "req", None) and self.req is not None and getattr(self.req, "url"):
            return self.req.url
        elif self.link is not None:
            return self.link.url_without_fragment
        return ""

    @name.default
    def get_name(self):
        # type: () -> Text
        loc = self.path or self.uri
        if loc and not self._uri_scheme:
            self._uri_scheme = "path" if self.path else "file"
        name = None
        hashed_loc = hashlib.sha256(loc.encode("utf-8")).hexdigest()
        hashed_name = hashed_loc[-7:]
        if getattr(self, "req", None) and self.req is not None and getattr(self.req, "name") and self.req.name is not None:
            if self.is_direct_url and self.req.name != hashed_name:
                return self.req.name
        if self.link and self.link.egg_fragment and self.link.egg_fragment != hashed_name:
            return self.link.egg_fragment
        elif self.link and self.link.is_wheel:
            from pip_shims import Wheel
            self._has_hashed_name = False
            return Wheel(self.link.filename).name
        elif self.link and ((self.link.scheme == "file" or self.editable) or (
            self.path and self.setup_path and os.path.isfile(str(self.setup_path))
        )):
            _ireq = None
            if self.editable:
                if self.setup_path:
                    line = pip_shims.shims.path_to_url(self.setup_py_dir)
                else:
                    line = pip_shims.shims.path_to_url(os.path.abspath(self.path))
                if self.extras:
                    line = "{0}[{1}]".format(line, ",".join(self.extras))
                _ireq = pip_shims.shims.install_req_from_editable(line)
            else:
                if self.setup_path:
                    line = Path(self.setup_py_dir).as_posix()
                else:
                    line = Path(os.path.abspath(self.path)).as_posix()
                if self.extras:
                    line = "{0}[{1}]".format(line, ",".join(self.extras))
                _ireq = pip_shims.shims.install_req_from_line(line)
            if getattr(self, "req", None) is not None:
                _ireq.req = copy.deepcopy(self.req)
            if self.extras and _ireq and not _ireq.extras:
                _ireq.extras = set(self.extras)
            from .setup_info import SetupInfo
            subdir = getattr(self, "subdirectory", None)
            if self.setup_info is not None:
                setupinfo = self.setup_info
            else:
                setupinfo = SetupInfo.from_ireq(_ireq, subdir=subdir)
            if setupinfo:
                self._setup_info = setupinfo
                self.setup_info.get_info()
                setupinfo_dict = setupinfo.as_dict()
                setup_name = setupinfo_dict.get("name", None)
                if setup_name:
                    name = setup_name
                    self._has_hashed_name = False
                build_requires = setupinfo_dict.get("build_requires")
                build_backend = setupinfo_dict.get("build_backend")
                if build_requires and not self.pyproject_requires:
                    self.pyproject_requires = tuple(build_requires)
                if build_backend and not self.pyproject_backend:
                    self.pyproject_backend = build_backend
        if not name or name.lower() == "unknown":
            self._has_hashed_name = True
            name = hashed_name
        name_in_link = getattr(self.link, "egg_fragment", "") if self.link else ""
        if not self._has_hashed_name and name_in_link != name and self.link is not None:
            self.link = create_link("{0}#egg={1}".format(self.link.url, name))
        if name is not None:
            return name
        return ""

    @link.default
    def get_link(self):
        # type: () -> pip_shims.shims.Link
        target = "{0}".format(self.uri)
        if hasattr(self, "name") and not self._has_hashed_name:
            target = "{0}#egg={1}".format(target, self.name)
        link = create_link(target)
        return link

    @req.default
    def get_requirement(self):
        # type: () -> RequirementType
        if self.name is None:
            if self._parsed_line is not None and self._parsed_line.name is not None:
                self.name = self._parsed_line.name
            else:
                raise ValueError(
                    "Failed to generate a requirement: missing name for {0!r}".format(self)
                )
        if self._parsed_line:
            try:
                # initialize specifiers to make sure we capture them
                self._parsed_line.specifiers
            except Exception:
                pass
            req = copy.deepcopy(self._parsed_line.requirement)
            return req

        req = init_requirement(normalize_name(self.name))
        req.editable = False
        if self.link is not None:
            req.line = self.link.url_without_fragment
        elif self.uri is not None:
            req.line = self.uri
        else:
            req.line = self.name
        if self.path and self.link and self.link.scheme.startswith("file"):
            req.local_file = True
            req.path = self.path
            if self.editable:
                req.url = None
            else:
                req.url = self.link.url_without_fragment
        else:
            req.local_file = False
            req.path = None
            req.url = self.link.url_without_fragment
        if self.editable:
            req.editable = True
        req.link = self.link
        return req

    @property
    def parsed_line(self):
        # type: () -> Optional[Line]
        if self._parsed_line is None:
            self._parsed_line = Line(self.line_part)
        return self._parsed_line

    @property
    def is_local(self):
        # type: () -> bool
        uri = getattr(self, "uri", None)
        if uri is None:
            if getattr(self, "path", None) and self.path is not None:
                uri = pip_shims.shims.path_to_url(os.path.abspath(self.path))
            elif getattr(self, "req", None) and self.req is not None and (
                getattr(self.req, "url") and self.req.url is not None
            ):
                uri = self.req.url
            if uri and is_file_url(uri):
                return True
        return False

    @property
    def is_remote_artifact(self):
        # type: () -> bool
        if self.link is None:
            return False
        return (
            any(
                self.link.scheme.startswith(scheme)
                for scheme in ("http", "https", "ftp", "ftps", "uri")
            )
            and (self.link.is_artifact or self.link.is_wheel)
            and not self.editable
        )

    @property
    def is_direct_url(self):
        # type: () -> bool
        if self._parsed_line is not None and self._parsed_line.is_direct_url:
            return True
        return self.is_remote_artifact

    @property
    def formatted_path(self):
        # type: () -> Optional[Text]
        if self.path:
            path = self.path
            if not isinstance(path, Path):
                path = Path(path)
            return path.as_posix()
        return None

    @classmethod
    def create(
        cls,
        path=None,  # type: Optional[Text]
        uri=None,  # type: Text
        editable=False,  # type: bool
        extras=None,  # type: Optional[Tuple[Text]]
        link=None,  # type: Link
        vcs_type=None,  # type: Optional[Any]
        name=None,  # type: Optional[Text]
        req=None,  # type: Optional[Any]
        line=None,  # type: Optional[Text]
        uri_scheme=None,  # type: Text
        setup_path=None,  # type: Optional[Any]
        relpath=None,  # type: Optional[Any]
        parsed_line=None,  # type: Optional[Line]
    ):
        # type: (...) -> FileRequirement
        if parsed_line is None and line is not None:
            parsed_line = Line(line)
        if relpath and not path:
            path = relpath
        if not path and uri and link is not None and link.scheme == "file":
            path = os.path.abspath(pip_shims.shims.url_to_path(unquote(uri)))
            try:
                path = get_converted_relative_path(path)
            except ValueError:  # Vistir raises a ValueError if it can't make a relpath
                path = path
        if line and not (uri_scheme and uri and link):
            vcs_type, uri_scheme, relpath, path, uri, link = cls.get_link_from_line(line)
        if not uri_scheme:
            uri_scheme = "path" if path else "file"
        if path and not uri:
            uri = unquote(pip_shims.shims.path_to_url(os.path.abspath(path)))
        if not link:
            link = cls.get_link_from_line(uri).link
        if not uri:
            uri = unquote(link.url_without_fragment)
        if not extras:
            extras = ()
        pyproject_path = None
        pyproject_requires = None
        pyproject_backend = None
        if path is not None:
            pyproject_requires = get_pyproject(path)
        if pyproject_requires is not None:
            pyproject_requires, pyproject_backend = pyproject_requires
            pyproject_requires = tuple(pyproject_requires)
        if path:
            setup_paths = get_setup_paths(path)
            if setup_paths["pyproject_toml"] is not None:
                pyproject_path = Path(setup_paths["pyproject_toml"])
            if setup_paths["setup_py"] is not None:
                setup_path = Path(setup_paths["setup_py"]).as_posix()
        if setup_path and isinstance(setup_path, Path):
            setup_path = setup_path.as_posix()
        creation_kwargs = {
            "editable": editable,
            "extras": extras,
            "pyproject_path": pyproject_path,
            "setup_path": setup_path if setup_path else None,
            "uri_scheme": uri_scheme,
            "link": link,
            "uri": uri,
            "pyproject_requires": pyproject_requires,
            "pyproject_backend": pyproject_backend,
            "path": path or relpath,
            "parsed_line": parsed_line
        }
        if vcs_type:
            creation_kwargs["vcs"] = vcs_type
        if name:
            creation_kwargs["name"] = name
        _line = None  # type: Optional[Text]
        ireq = None  # type: Optional[InstallRequirement]
        setup_info = None  # type: Optional[SetupInfo]
        if parsed_line:
            if parsed_line.name:
                name = parsed_line.name
            if parsed_line.setup_info:
                name = parsed_line.setup_info.as_dict().get("name", name)
        if not name or not parsed_line:
            if link is not None and link.url_without_fragment is not None:
                _line = unquote(link.url_without_fragment)
                if name:
                    _line = "{0}#egg={1}".format(_line, name)
                if extras and extras_to_string(extras) not in _line:
                    _line = "{0}[{1}]".format(_line, ",".join(sorted(set(extras))))
            elif uri is not None:
                _line = unquote(uri)
            else:
                _line = unquote(line)
            if editable:
                if extras and extras_to_string(extras) not in _line and (
                    (link and link.scheme == "file") or (uri and uri.startswith("file"))
                    or (not uri and not link)
                ):
                    _line = "{0}[{1}]".format(_line, ",".join(sorted(set(extras))))
                if ireq is None:
                    ireq = pip_shims.shims.install_req_from_editable(_line)
            else:
                _line = path if (uri_scheme and uri_scheme == "path") else _line
                if extras and extras_to_string(extras) not in _line:
                    _line = "{0}[{1}]".format(_line, ",".join(sorted(set(extras))))
                if ireq is None:
                    ireq = pip_shims.shims.install_req_from_line(_line)
                if editable:
                    _line = "-e {0}".format(editable)
            parsed_line = Line(_line)
            if ireq is None:
                ireq = parsed_line.ireq
            if extras and ireq is not None and not ireq.extras:
                ireq.extras = set(extras)
            if setup_info is None:
                setup_info = SetupInfo.from_ireq(ireq)
            setupinfo_dict = setup_info.as_dict()
            setup_name = setupinfo_dict.get("name", None)
            if setup_name is not None:
                name = setup_name
                build_requires = setupinfo_dict.get("build_requires", ())
                build_backend = setupinfo_dict.get("build_backend", "")
                if not creation_kwargs.get("pyproject_requires") and build_requires:
                    creation_kwargs["pyproject_requires"] = tuple(build_requires)
                if not creation_kwargs.get("pyproject_backend") and build_backend:
                    creation_kwargs["pyproject_backend"] = build_backend
        if setup_info is None and parsed_line and parsed_line.setup_info:
            setup_info = parsed_line.setup_info
        creation_kwargs["setup_info"] = setup_info
        if path or relpath:
            creation_kwargs["path"] = relpath if relpath else path
        if req is not None:
            creation_kwargs["req"] = req
        creation_req = creation_kwargs.get("req")
        if creation_kwargs.get("req") is not None:
            creation_req_line = getattr(creation_req, "line", None)
            if creation_req_line is None and line is not None:
                creation_kwargs["req"].line = line  # type: ignore
        if parsed_line and parsed_line.name:
            if name and len(parsed_line.name) != 7 and len(name) == 7:
                name = parsed_line.name
        if name:
            creation_kwargs["name"] = name
        cls_inst = cls(**creation_kwargs)  # type: ignore
        return cls_inst

    @classmethod
    def from_line(cls, line, extras=None, parsed_line=None):
        # type: (Text, Optional[Tuple[Text]], Optional[Line]) -> FileRequirement
        line = line.strip('"').strip("'")
        link = None
        path = None
        editable = line.startswith("-e ")
        line = line.split(" ", 1)[1] if editable else line
        setup_path = None
        name = None
        req = None
        if not extras:
            extras = ()
        else:
            extras = tuple(extras)
        if not any([is_installable_file(line), is_valid_url(line), is_file_url(line)]):
            try:
                req = init_requirement(line)
            except Exception:
                raise RequirementError(
                    "Supplied requirement is not installable: {0!r}".format(line)
                )
            else:
                name = getattr(req, "name", None)
                line = getattr(req, "url", None)
        vcs_type, prefer, relpath, path, uri, link = cls.get_link_from_line(line)
        arg_dict = {
            "path": relpath if relpath else path,
            "uri": unquote(link.url_without_fragment),
            "link": link,
            "editable": editable,
            "setup_path": setup_path,
            "uri_scheme": prefer,
            "line": line,
            "extras": extras,
            # "name": name,
        }
        if req is not None:
            arg_dict["req"] = req
        if parsed_line is not None:
            arg_dict["parsed_line"] = parsed_line
        if link and link.is_wheel:
            from pip_shims import Wheel

            arg_dict["name"] = Wheel(link.filename).name
        elif name:
            arg_dict["name"] = name
        elif link.egg_fragment:
            arg_dict["name"] = link.egg_fragment
        return cls.create(**arg_dict)

    @classmethod
    def from_pipfile(cls, name, pipfile):
        # type: (Text, Dict[Text, Any]) -> FileRequirement
        # Parse the values out. After this dance we should have two variables:
        # path - Local filesystem path.
        # uri - Absolute URI that is parsable with urlsplit.
        # One of these will be a string; the other would be None.
        uri = pipfile.get("uri")
        fil = pipfile.get("file")
        path = pipfile.get("path")
        if path:
            if isinstance(path, Path) and not path.is_absolute():
                path = get_converted_relative_path(path.as_posix())
            elif not os.path.isabs(path):
                path = get_converted_relative_path(path)
        if path and uri:
            raise ValueError("do not specify both 'path' and 'uri'")
        if path and fil:
            raise ValueError("do not specify both 'path' and 'file'")
        uri = uri or fil

        # Decide that scheme to use.
        # 'path' - local filesystem path.
        # 'file' - A file:// URI (possibly with VCS prefix).
        # 'uri' - Any other URI.
        if path:
            uri_scheme = "path"
        else:
            # URI is not currently a valid key in pipfile entries
            # see https://github.com/pypa/pipfile/issues/110
            uri_scheme = "file"

        if not uri:
            uri = pip_shims.shims.path_to_url(path)
        link = cls.get_link_from_line(uri).link
        arg_dict = {
            "name": name,
            "path": path,
            "uri": unquote(link.url_without_fragment),
            "editable": pipfile.get("editable", False),
            "link": link,
            "uri_scheme": uri_scheme,
            "extras": pipfile.get("extras", None),
        }

        extras = pipfile.get("extras", ())
        if extras:
            extras = tuple(extras)
        line = ""
        if pipfile.get("editable", False) and uri_scheme == "path":
            line = "{0}".format(path)
            if extras:
                line = "{0}{1}".format(line, extras_to_string(extras))
        else:
            if name:
                if extras:
                    line_name = "{0}{1}".format(name, extras_to_string(extras))
                else:
                    line_name = "{0}".format(name)
                line = "{0}#egg={1}".format(unquote(link.url_without_fragment), line_name)
            else:
                line = unquote(link.url)
                if extras:
                    line = "{0}{1}".format(line, extras_to_string(extras))
            if "subdirectory" in pipfile:
                arg_dict["subdirectory"] = pipfile["subdirectory"]
                line = "{0}&subdirectory={1}".format(line, pipfile["subdirectory"])
        if pipfile.get("editable", False):
            line = "-e {0}".format(line)
        arg_dict["line"] = line
        return cls.create(**arg_dict)

    @property
    def line_part(self):
        # type: () -> Text
        link_url = None  # type: Optional[Text]
        seed = None  # type: Optional[Text]
        if self.link is not None:
            link_url = unquote(self.link.url_without_fragment)
        if self._uri_scheme and self._uri_scheme == "path":
            # We may need any one of these for passing to pip
            seed = self.path or link_url or self.uri
        elif (self._uri_scheme and self._uri_scheme == "file") or (
            (self.link.is_artifact or self.link.is_wheel) and self.link.url
        ):
            seed = link_url or self.uri
        # add egg fragments to remote artifacts (valid urls only)
        if not self._has_hashed_name and self.is_remote_artifact and seed is not None:
            seed += "#egg={0}".format(self.name)
        editable = "-e " if self.editable else ""
        if seed is None:
            raise ValueError("Could not calculate url for {0!r}".format(self))
        return "{0}{1}".format(editable, seed)

    @property
    def pipfile_part(self):
        # type: () -> Dict[Text, Dict[Text, Any]]
        excludes = [
            "_base_line", "_has_hashed_name", "setup_path", "pyproject_path", "_uri_scheme",
            "pyproject_requires", "pyproject_backend", "_setup_info", "_parsed_line"
        ]
        filter_func = lambda k, v: bool(v) is True and k.name not in excludes  # noqa
        pipfile_dict = attr.asdict(self, filter=filter_func).copy()
        name = pipfile_dict.pop("name", None)
        if name is None:
            if self.name:
                name = self.name
            elif self.parsed_line and self.parsed_line.name:
                name = self.name = self.parsed_line.name
            elif self.setup_info and self.setup_info.name:
                name = self.name = self.setup_info.name
        if "_uri_scheme" in pipfile_dict:
            pipfile_dict.pop("_uri_scheme")
        # For local paths and remote installable artifacts (zipfiles, etc)
        collision_keys = {"file", "uri", "path"}
        collision_order = ["file", "uri", "path"]  # type: List[Text]
        key_match = next(iter(k for k in collision_order if k in pipfile_dict.keys()))
        if self._uri_scheme:
            dict_key = self._uri_scheme
            target_key = (
                dict_key
                if dict_key in pipfile_dict
                else key_match
            )
            if target_key is not None:
                winning_value = pipfile_dict.pop(target_key)
                collisions = [k for k in collision_keys if k in pipfile_dict]
                for key in collisions:
                    pipfile_dict.pop(key)
                pipfile_dict[dict_key] = winning_value
        elif (
            self.is_remote_artifact
            or (self.link is not None and self.link.is_artifact)
            and (self._uri_scheme and self._uri_scheme == "file")
        ):
            dict_key = "file"
            # Look for uri first because file is a uri format and this is designed
            # to make sure we add file keys to the pipfile as a replacement of uri
            if key_match is not None:
                winning_value = pipfile_dict.pop(key_match)
            key_to_remove = (k for k in collision_keys if k in pipfile_dict)
            for key in key_to_remove:
                pipfile_dict.pop(key)
            pipfile_dict[dict_key] = winning_value
        else:
            collisions = [key for key in collision_order if key in pipfile_dict.keys()]
            if len(collisions) > 1:
                for k in collisions[1:]:
                    pipfile_dict.pop(k)
        return {name: pipfile_dict}


@attr.s(slots=True, hash=True)
class VCSRequirement(FileRequirement):
    #: Whether the repository is editable
    editable = attr.ib(default=None)  # type: Optional[bool]
    #: URI for the repository
    uri = attr.ib(default=None)  # type: Optional[Text]
    #: path to the repository, if it's local
    path = attr.ib(default=None, validator=attr.validators.optional(validate_path))  # type: Optional[Text]
    #: vcs type, i.e. git/hg/svn
    vcs = attr.ib(validator=attr.validators.optional(validate_vcs), default=None)  # type: Optional[Text]
    #: vcs reference name (branch / commit / tag)
    ref = attr.ib(default=None)  # type: Optional[Text]
    #: Subdirectory to use for installation if applicable
    subdirectory = attr.ib(default=None)  # type: Optional[Text]
    _repo = attr.ib(default=None)  # type: Optional[VCSRepository]
    _base_line = attr.ib(default=None)  # type: Optional[Text]
    name = attr.ib()  # type: Text
    link = attr.ib()  # type: Optional[pip_shims.shims.Link]
    req = attr.ib()  # type: Optional[RequirementType]

    def __attrs_post_init__(self):
        # type: () -> None
        if not self.uri:
            if self.path:
                self.uri = pip_shims.shims.path_to_url(self.path)
        if self.uri is not None:
            split = urllib_parse.urlsplit(self.uri)
            scheme, rest = split[0], split[1:]
            vcs_type = ""
            if "+" in scheme:
                vcs_type, scheme = scheme.split("+", 1)
                vcs_type = "{0}+".format(vcs_type)
            new_uri = urllib_parse.urlunsplit((scheme,) + rest[:-1] + ("",))
            new_uri = "{0}{1}".format(vcs_type, new_uri)
            self.uri = new_uri

    @link.default
    def get_link(self):
        # type: () -> pip_shims.shims.Link
        uri = self.uri if self.uri else pip_shims.shims.path_to_url(self.path)
        vcs_uri = build_vcs_uri(
            self.vcs,
            add_ssh_scheme_to_git_uri(uri),
            name=self.name,
            ref=self.ref,
            subdirectory=self.subdirectory,
            extras=self.extras,
        )
        return self.get_link_from_line(vcs_uri).link

    @name.default
    def get_name(self):
        # type: () -> Optional[Text]
        return (
            self.link.egg_fragment or self.req.name
            if getattr(self, "req", None)
            else super(VCSRequirement, self).get_name()
        )

    @property
    def vcs_uri(self):
        # type: () -> Optional[Text]
        uri = self.uri
        if not any(uri.startswith("{0}+".format(vcs)) for vcs in VCS_LIST):
            uri = "{0}+{1}".format(self.vcs, uri)
        return uri

    @property
    def setup_info(self):
        if self._parsed_line and self._parsed_line.setup_info:
            if not self._parsed_line.setup_info.name:
                self._parsed_line._setup_info.get_info()
            return self._parsed_line.setup_info
        if self._repo:
            from .setup_info import SetupInfo
            self._setup_info = SetupInfo.from_ireq(Line(self._repo.checkout_directory).ireq)
            self._setup_info.get_info()
            return self._setup_info
        ireq = self.parsed_line.ireq
        from .setup_info import SetupInfo
        self._setup_info = SetupInfo.from_ireq(ireq)
        return self._setup_info

    @setup_info.setter
    def setup_info(self, setup_info):
        self._setup_info = setup_info
        if self._parsed_line:
            self._parsed_line.setup_info = setup_info

    @req.default
    def get_requirement(self):
        # type: () -> PackagingRequirement
        name = self.name or self.link.egg_fragment
        url = None
        if self.uri:
            url = self.uri
        elif self.link is not None:
            url = self.link.url_without_fragment
        if not name:
            raise ValueError(
                "pipenv requires an #egg fragment for version controlled "
                "dependencies. Please install remote dependency "
                "in the form {0}#egg=<package-name>.".format(url)
            )
        req = init_requirement(canonicalize_name(self.name))
        req.editable = self.editable
        if not getattr(req, "url"):
            if url is not None:
                url = add_ssh_scheme_to_git_uri(url)
            elif self.uri is not None:
                url = self.parse_link_from_line(self.uri).link.url_without_fragment
            if url.startswith("git+file:/") and not url.startswith("git+file:///"):
                url = url.replace("git+file:/", "git+file:///")
            if url:
                req.url = url
        line = url if url else self.vcs_uri
        if self.editable:
            line = "-e {0}".format(line)
        req.line = line
        if self.ref:
            req.revision = self.ref
        if self.extras:
            req.extras = self.extras
        req.vcs = self.vcs
        if self.path and self.link and self.link.scheme.startswith("file"):
            req.local_file = True
            req.path = self.path
        req.link = self.link
        if (
            self.uri != unquote(self.link.url_without_fragment)
            and "git+ssh://" in self.link.url
            and "git+git@" in self.uri
        ):
            req.line = self.uri
            url = self.link.url_without_fragment
            if url.startswith("git+file:/") and not url.startswith("git+file:///"):
                url = url.replace("git+file:/", "git+file:///")
            req.url = url
        return req

    @property
    def repo(self):
        # type: () -> VCSRepository
        if self._repo is None:
            if self._parsed_line and self._parsed_line.vcsrepo:
                self._repo = self._parsed_line.vcsrepo
            else:
                self._repo = self.get_vcs_repo()
                if self._parsed_line:
                    self._parsed_line.vcsrepo = self._repo
        return self._repo

    def get_checkout_dir(self, src_dir=None):
        # type: (Optional[Text]) -> Text
        src_dir = os.environ.get("PIP_SRC", None) if not src_dir else src_dir
        checkout_dir = None
        if self.is_local:
            path = self.path
            if not path:
                path = pip_shims.shims.url_to_path(self.uri)
            if path and os.path.exists(path):
                checkout_dir = os.path.abspath(path)
                return checkout_dir
        if src_dir is not None:
            checkout_dir = os.path.join(os.path.abspath(src_dir), self.name)
            mkdir_p(src_dir)
            return checkout_dir
        return os.path.join(create_tracked_tempdir(prefix="requirementslib"), self.name)

    def get_vcs_repo(self, src_dir=None, checkout_dir=None):
        # type: (Optional[Text], Optional[Text]) -> VCSRepository
        from .vcs import VCSRepository

        if checkout_dir is None:
            checkout_dir = self.get_checkout_dir(src_dir=src_dir)
        vcsrepo = VCSRepository(
            url=self.link.url,
            name=self.name,
            ref=self.ref if self.ref else None,
            checkout_directory=checkout_dir,
            vcs_type=self.vcs,
            subdirectory=self.subdirectory,
        )
        if not self.is_local:
            vcsrepo.obtain()
        pyproject_info = None
        if self.subdirectory:
            self.setup_path = os.path.join(checkout_dir, self.subdirectory, "setup.py")
            self.pyproject_path = os.path.join(checkout_dir, self.subdirectory, "pyproject.toml")
            pyproject_info = get_pyproject(os.path.join(checkout_dir, self.subdirectory))
        else:
            self.setup_path = os.path.join(checkout_dir, "setup.py")
            self.pyproject_path = os.path.join(checkout_dir, "pyproject.toml")
            pyproject_info = get_pyproject(checkout_dir)
        if pyproject_info is not None:
            pyproject_requires, pyproject_backend = pyproject_info
            self.pyproject_requires = tuple(pyproject_requires)
            self.pyproject_backend = pyproject_backend
        return vcsrepo

    def get_commit_hash(self):
        # type: () -> Text
        hash_ = None
        hash_ = self.repo.get_commit_hash()
        return hash_

    def update_repo(self, src_dir=None, ref=None):
        # type: (Optional[Text], Optional[Text]) -> Text
        if ref:
            self.ref = ref
        else:
            if self.ref:
                ref = self.ref
        repo_hash = None
        if not self.is_local and ref is not None:
            self.repo.checkout_ref(ref)
        repo_hash = self.repo.get_commit_hash()
        self.req.revision = repo_hash
        return repo_hash

    @contextmanager
    def locked_vcs_repo(self, src_dir=None):
        # type: (Optional[Text]) -> Generator[VCSRepository, None, None]
        if not src_dir:
            src_dir = create_tracked_tempdir(prefix="requirementslib-", suffix="-src")
        vcsrepo = self.get_vcs_repo(src_dir=src_dir)
        if not self.req:
            if self.parsed_line is not None:
                self.req = self.parsed_line.requirement
            else:
                self.req = self.get_requirement()
        revision = self.req.revision = vcsrepo.get_commit_hash()

        # Remove potential ref in the end of uri after ref is parsed
        if "@" in self.link.show_url and "@" in self.uri:
            uri, ref = split_ref_from_uri(self.uri)
            checkout = revision
            if checkout and ref and ref in checkout:
                self.uri = uri
        orig_repo = self._repo
        self._repo = vcsrepo
        if self._parsed_line:
            self._parsed_line.vcsrepo = vcsrepo
        if self._setup_info:
            _old_setup_info = self._setup_info
            self._setup_info = attr.evolve(
                self._setup_info, requirements=(), _extras_requirements=(),
                build_requires=(), setup_requires=(), version=None, metadata=None
            )
        if self.parsed_line:
            self._parsed_line.vcsrepo = vcsrepo
            # self._parsed_line._specifier = "=={0}".format(self.setup_info.version)
            # self._parsed_line.specifiers = self._parsed_line._specifier
        if self.req:
            self.req.specifier = SpecifierSet("=={0}".format(self.setup_info.version))
        try:
            yield self._repo
        except Exception:
            self._repo = orig_repo
            raise

    @classmethod
    def from_pipfile(cls, name, pipfile):
        # type: (Text, Dict[Text, Union[List[Text], Text, bool]]) -> VCSRequirement
        creation_args = {}
        pipfile_keys = [
            k
            for k in (
                "ref",
                "vcs",
                "subdirectory",
                "path",
                "editable",
                "file",
                "uri",
                "extras",
            )
            + VCS_LIST
            if k in pipfile
        ]
        for key in pipfile_keys:
            if key == "extras":
                extras = pipfile.get(key, None)
                if extras:
                    pipfile[key] = sorted(dedup([extra.lower() for extra in extras]))
            if key in VCS_LIST:
                creation_args["vcs"] = key
                target = pipfile.get(key)
                drive, path = os.path.splitdrive(target)
                if (
                    not drive
                    and not os.path.exists(target)
                    and (
                        is_valid_url(target)
                        or is_file_url(target)
                        or target.startswith("git@")
                    )
                ):
                    creation_args["uri"] = target
                else:
                    creation_args["path"] = target
                    if os.path.isabs(target):
                        creation_args["uri"] = pip_shims.shims.path_to_url(target)
            else:
                creation_args[key] = pipfile.get(key)
        creation_args["name"] = name
        cls_inst = cls(**creation_args)
        return cls_inst

    @classmethod
    def from_line(cls, line, editable=None, extras=None, parsed_line=None):
        # type: (Text, Optional[bool], Optional[Tuple[Text]], Optional[Line]) -> VCSRequirement
        relpath = None
        if parsed_line is None:
            parsed_line = Line(line)
        if editable:
            parsed_line.editable = editable
        if extras:
            parsed_line.extras = extras
        if line.startswith("-e "):
            editable = True
            line = line.split(" ", 1)[1]
        if "@" in line:
            parsed = urllib_parse.urlparse(add_ssh_scheme_to_git_uri(line))
            if not parsed.scheme:
                possible_name, _, line = line.partition("@")
                possible_name = possible_name.strip()
                line = line.strip()
                possible_name, extras = pip_shims.shims._strip_extras(possible_name)
                name = possible_name
                line = "{0}#egg={1}".format(line, name)
        vcs_type, prefer, relpath, path, uri, link = cls.get_link_from_line(line)
        if not extras and link.egg_fragment:
            name, extras = pip_shims.shims._strip_extras(link.egg_fragment)
        else:
            name, _ = pip_shims.shims._strip_extras(link.egg_fragment)
        if extras:
            extras = parse_extras(extras)
        else:
            line, extras = pip_shims.shims._strip_extras(line)
        if extras:
            extras = tuple(extras)
        subdirectory = link.subdirectory_fragment
        ref = None
        if uri:
            uri, ref = split_ref_from_uri(uri)
        if path is not None and "@" in path:
            path, _ref = split_ref_from_uri(path)
            if ref is None:
                ref = _ref
        if relpath and "@" in relpath:
            relpath, ref = split_ref_from_uri(relpath)

        creation_args = {
            "name": name if name else parsed_line.name,
            "path": relpath or path,
            "editable": editable,
            "extras": extras,
            "link": link,
            "vcs_type": vcs_type,
            "line": line,
            "uri": uri,
            "uri_scheme": prefer,
            "parsed_line": parsed_line
        }
        if relpath:
            creation_args["relpath"] = relpath
        # return cls.create(**creation_args)
        cls_inst = cls(
            name=name,
            ref=ref,
            vcs=vcs_type,
            subdirectory=subdirectory,
            link=link,
            path=relpath or path,
            editable=editable,
            uri=uri,
            extras=extras,
            base_line=line,
            parsed_line=parsed_line
        )
        if cls_inst.req and (
            cls_inst._parsed_line.ireq and not cls_inst.parsed_line.ireq.req
        ):
            cls_inst._parsed_line._ireq.req = cls_inst.req
        return cls_inst

    @property
    def line_part(self):
        # type: () -> Text
        """requirements.txt compatible line part sans-extras"""
        if self.is_local:
            base_link = self.link
            if not self.link:
                base_link = self.get_link()
            final_format = (
                "{{0}}#egg={0}".format(base_link.egg_fragment)
                if base_link.egg_fragment
                else "{0}"
            )
            base = final_format.format(self.vcs_uri)
        elif self._parsed_line is not None and self._parsed_line.is_direct_url:
            return self._parsed_line.line_with_prefix
        elif getattr(self, "_base_line", None):
            base = self._base_line
        else:
            base = getattr(self, "link", self.get_link()).url
        if base and self.extras and extras_to_string(self.extras) not in base:
            if self.subdirectory:
                base = "{0}".format(self.get_link().url)
            else:
                base = "{0}{1}".format(base, extras_to_string(sorted(self.extras)))
        if "git+file:/" in base and "git+file:///" not in base:
            base = base.replace("git+file:/", "git+file:///")
        if self.editable and not base.startswith("-e "):
            base = "-e {0}".format(base)
        return base

    @staticmethod
    def _choose_vcs_source(pipfile):
        # type: (Dict[Text, Union[List[Text], Text, bool]]) -> Dict[Text, Union[List[Text], Text, bool]]
        src_keys = [k for k in pipfile.keys() if k in ["path", "uri", "file"]]
        if src_keys:
            chosen_key = first(src_keys)
            vcs_type = pipfile.pop("vcs")
            _, pipfile_url = split_vcs_method_from_uri(pipfile.get(chosen_key))
            pipfile[vcs_type] = pipfile_url
            for removed in src_keys:
                pipfile.pop(removed)
        return pipfile

    @property
    def pipfile_part(self):
        # type: () -> Dict[Text, Dict[Text, Union[List[Text], Text, bool]]]
        excludes = [
            "_repo", "_base_line", "setup_path", "_has_hashed_name", "pyproject_path",
            "pyproject_requires", "pyproject_backend", "_setup_info", "_parsed_line",
            "_uri_scheme"
        ]
        filter_func = lambda k, v: bool(v) is True and k.name not in excludes  # noqa
        pipfile_dict = attr.asdict(self, filter=filter_func).copy()
        name = pipfile_dict.pop("name", None)
        if name is None:
            if self.name:
                name = self.name
            elif self.parsed_line and self.parsed_line.name:
                name = self.name = self.parsed_line.name
            elif self.setup_info and self.setup_info.name:
                name = self.name = self.setup_info.name
        if "vcs" in pipfile_dict:
            pipfile_dict = self._choose_vcs_source(pipfile_dict)
        name, _ = pip_shims.shims._strip_extras(name)
        return {name: pipfile_dict}


@attr.s(cmp=True, hash=True)
class Requirement(object):
    _name = attr.ib(cmp=True)  # type: Text
    vcs = attr.ib(default=None, validator=attr.validators.optional(validate_vcs), cmp=True)  # type: Optional[Text]
    req = attr.ib(default=None, cmp=True)  # type: Optional[Union[VCSRequirement, FileRequirement, NamedRequirement]]
    markers = attr.ib(default=None, cmp=True)  # type: Optional[Text]
    _specifiers = attr.ib(validator=attr.validators.optional(validate_specifiers), cmp=True)  # type: Optional[Text]
    index = attr.ib(default=None, cmp=True)  # type: Optional[Text]
    editable = attr.ib(default=None, cmp=True)  # type: Optional[bool]
    hashes = attr.ib(factory=frozenset, converter=frozenset, cmp=True)  # type: Optional[Tuple[Text]]
    extras = attr.ib(default=attr.Factory(tuple), cmp=True)  # type: Optional[Tuple[Text]]
    abstract_dep = attr.ib(default=None, cmp=False)  # type: Optional[AbstractDependency]
    _line_instance = attr.ib(default=None, cmp=False)  # type: Optional[Line]
    _ireq = attr.ib(default=None, cmp=False)  # type: Optional[pip_shims.InstallRequirement]

    def __hash__(self):
        return hash(self.as_line())

    @_name.default
    def get_name(self):
        # type: () -> Optional[Text]
        return self.req.name

    @property
    def name(self):
        # type: () -> Optional[Text]
        if self._name is not None:
            return self._name
        name = None
        if self.req and self.req.name:
            name = self.req.name
        elif self.req and self.is_file_or_url and self.req.setup_info:
            name = self.req.setup_info.name
        self._name = name
        return name

    @property
    def requirement(self):
        # type: () -> Optional[PackagingRequirement]
        return self.req.req

    def add_hashes(self, hashes):
        # type: (Union[List, Set, Tuple]) -> Requirement
        if isinstance(hashes, six.string_types):
            new_hashes = set(self.hashes).add(hashes)
        else:
            new_hashes = set(self.hashes) | set(hashes)
        return attr.evolve(self, hashes=frozenset(new_hashes))

    def get_hashes_as_pip(self, as_list=False):
        # type: (bool) -> Union[Text, List[Text]]
        if self.hashes:
            if as_list:
                return [HASH_STRING.format(h) for h in self.hashes]
            return "".join([HASH_STRING.format(h) for h in self.hashes])
        return "" if not as_list else []

    @property
    def hashes_as_pip(self):
        # type: () -> Union[Text, List[Text]]
        self.get_hashes_as_pip()

    @property
    def markers_as_pip(self):
        # type: () -> Text
        if self.markers:
            return " ; {0}".format(self.markers).replace('"', "'")

        return ""

    @property
    def extras_as_pip(self):
        # type: () -> Text
        if self.extras:
            return "[{0}]".format(
                ",".join(sorted([extra.lower() for extra in self.extras]))
            )

        return ""

    @cached_property
    def commit_hash(self):
        # type: () -> Optional[Text]
        if not self.is_vcs:
            return None
        commit_hash = None
        with self.req.locked_vcs_repo() as repo:
            commit_hash = repo.get_commit_hash()
        return commit_hash

    @_specifiers.default
    def get_specifiers(self):
        # type: () -> Text
        if self.req and self.req.req and self.req.req.specifier:
            return specs_to_string(self.req.req.specifier)
        return ""

    def update_name_from_path(self, path):
        from .setup_info import get_metadata
        metadata = get_metadata(path)
        name = self.name
        if metadata is not None:
            name = metadata.get("name")
        if name is not None:
            if self.req.name is None:
                self.req.name = name
            if self.req.req and self.req.req.name is None:
                self.req.req.name = name
            if self._line_instance._name is None:
                self._line_instance.name = name
            if self.req._parsed_line._name is None:
                self.req._parsed_line.name = name
            if self.req._setup_info and self.req._setup_info.name is None:
                self.req._setup_info.name = name

    @property
    def line_instance(self):
        # type: () -> Optional[Line]
        if self._line_instance is None:
            if self.req._parsed_line is not None:
                self._line_instance = self.req.parsed_line
            else:
                include_extras = True
                include_specifiers = True
                if self.is_vcs:
                    include_extras = False
                if self.is_file_or_url or self.is_vcs or not self._specifiers:
                    include_specifiers = False

                parts = [
                    self.req.line_part,
                    self.extras_as_pip if include_extras else "",
                    self._specifiers if include_specifiers else "",
                    self.markers_as_pip,
                ]
                line = "".join(parts)
                if line is None:
                    return None
                self._line_instance = Line(line)
        return self._line_instance

    @line_instance.setter
    def line_instance(self, line_instance):
        # type: (Line) -> None
        if self.req and not self.req._parsed_line:
            self.req._parsed_line = line_instance
        self._line_instance = line_instance

    @property
    def specifiers(self):
        # type: () -> Optional[Text]
        if self._specifiers:
            return self._specifiers
        else:
            specs = self.get_specifiers()
            if specs:
                self._specifiers = specs
                return specs
        if self.is_named and not self._specifiers:
            self._specifiers = self.req.version
        elif not self.editable and not self.is_named:
            if self.line_instance and self.line_instance.setup_info and self.line_instance.setup_info.version:
                self._specifiers = "=={0}".format(self.req.setup_info.version)
        elif self.req.parsed_line.specifiers and not self._specifiers:
            self._specifiers = specs_to_string(self.req.parsed_line.specifiers)
        elif self.line_instance.specifiers and not self._specifiers:
            self._specifiers = specs_to_string(self.line_instance.specifiers)
        elif not self._specifiers and (self.is_file_or_url or self.is_vcs):
            try:
                setupinfo_dict = self.run_requires()
            except Exception:
                setupinfo_dict = None
            if setupinfo_dict is not None:
                self._specifiers = "=={0}".format(setupinfo_dict.get("version"))
        if self._specifiers:
            specset = SpecifierSet(self._specifiers)
            if self.line_instance and not self.line_instance.specifiers:
                self.line_instance.specifiers = specset
            if self.req and self.req.parsed_line and not self.req.parsed_line.specifiers:
                self.req._parsed_line.specifiers = specset
            if self.req and self.req.req and not self.req.req.specifier:
                self.req.req.specifier = specset
        return self._specifiers

    @property
    def is_vcs(self):
        # type: () -> bool
        return isinstance(self.req, VCSRequirement)

    @property
    def build_backend(self):
        # type: () -> Optional[Text]
        if self.is_vcs or (self.is_file_or_url and (
                self.req is not None and self.req.is_local)):
            setup_info = self.run_requires()
            build_backend = setup_info.get("build_backend")
            return build_backend
        return "setuptools.build_meta"

    @property
    def uses_pep517(self):
        # type: () -> bool
        if self.build_backend:
            return True
        return False

    @property
    def is_file_or_url(self):
        # type: () -> bool
        return isinstance(self.req, FileRequirement)

    @property
    def is_named(self):
        # type: () -> bool
        return isinstance(self.req, NamedRequirement)

    @property
    def is_wheel(self):
        # type: () -> bool
        if not self.is_named and (
            self.req is not None and
            self.req.link is not None and
            self.req.link.is_wheel
        ):
            return True
        return False

    @property
    def normalized_name(self):
        # type: () -> Text
        return canonicalize_name(self.name)

    def copy(self):
        return attr.evolve(self)

    @classmethod
    @lru_cache()
    def from_line(cls, line):
        # type: (Text) -> Requirement
        if isinstance(line, pip_shims.shims.InstallRequirement):
            line = format_requirement(line)
        parsed_line = Line(line)
        r = None  # type: Optional[Union[VCSRequirement, FileRequirement, NamedRequirement]]
        if ((parsed_line.is_file and parsed_line.is_installable) or parsed_line.is_url) and not parsed_line.is_vcs:
            r = file_req_from_parsed_line(parsed_line)
        elif parsed_line.is_vcs:
            r = vcs_req_from_parsed_line(parsed_line)
        elif line == "." and not is_installable_file(line):
            raise RequirementError(
                "Error parsing requirement %s -- are you sure it is installable?" % line
            )
        else:
            r = named_req_from_parsed_line(parsed_line)
        req_markers = None
        if parsed_line.markers:
            req_markers = PackagingRequirement("fakepkg; {0}".format(parsed_line.markers))
        if r is not None and r.req is not None:
            r.req.marker = getattr(req_markers, "marker", None) if req_markers else None
        args = {
            "name": r.name,
            "vcs": parsed_line.vcs,
            "req": r,
            "markers": parsed_line.markers,
            "editable": parsed_line.editable,
            "line_instance": parsed_line
        }
        if parsed_line.extras:
            extras = tuple(sorted(dedup([extra.lower() for extra in parsed_line.extras])))
            args["extras"] = extras
            if r is not None:
                r.extras = extras
            elif r is not None and r.extras is not None:
                args["extras"] = tuple(sorted(dedup([extra.lower() for extra in r.extras])))  # type: ignore
            if r.req is not None:
                r.req.extras = args["extras"]
        if parsed_line.hashes:
            args["hashes"] = tuple(parsed_line.hashes)  # type: ignore
        cls_inst = cls(**args)  # type: ignore
        return cls_inst

    @classmethod
    def from_ireq(cls, ireq):
        return cls.from_line(format_requirement(ireq))

    @classmethod
    def from_metadata(cls, name, version, extras, markers):
        return cls.from_ireq(
            make_install_requirement(name, version, extras=extras, markers=markers)
        )

    @classmethod
    def from_pipfile(cls, name, pipfile):
        from .markers import PipenvMarkers

        _pipfile = {}
        if hasattr(pipfile, "keys"):
            _pipfile = dict(pipfile).copy()
        _pipfile["version"] = get_version(pipfile)
        vcs = first([vcs for vcs in VCS_LIST if vcs in _pipfile])
        if vcs:
            _pipfile["vcs"] = vcs
            r = VCSRequirement.from_pipfile(name, pipfile)
        elif any(key in _pipfile for key in ["path", "file", "uri"]):
            r = FileRequirement.from_pipfile(name, pipfile)
        else:
            r = NamedRequirement.from_pipfile(name, pipfile)
        markers = PipenvMarkers.from_pipfile(name, _pipfile)
        req_markers = None
        if markers:
            markers = str(markers)
            req_markers = PackagingRequirement("fakepkg; {0}".format(markers))
            if r.req is not None:
                r.req.marker = req_markers.marker
        extras = _pipfile.get("extras")
        if r.req:
            if r.req.specifier:
                r.req.specifier = SpecifierSet(_pipfile["version"])
            r.req.extras = (
                tuple(sorted(dedup([extra.lower() for extra in extras]))) if extras else ()
            )
        args = {
            "name": r.name,
            "vcs": vcs,
            "req": r,
            "markers": markers,
            "extras": tuple(_pipfile.get("extras", ())),
            "editable": _pipfile.get("editable", False),
            "index": _pipfile.get("index"),
        }
        if any(key in _pipfile for key in ["hash", "hashes"]):
            args["hashes"] = _pipfile.get("hashes", [pipfile.get("hash")])
        cls_inst = cls(**args)
        return cls_inst

    def as_line(
        self,
        sources=None,
        include_hashes=True,
        include_extras=True,
        include_markers=True,
        as_list=False,
    ):
        """Format this requirement as a line in requirements.txt.

        If ``sources`` provided, it should be an sequence of mappings, containing
        all possible sources to be used for this requirement.

        If ``sources`` is omitted or falsy, no index information will be included
        in the requirement line.
        """

        include_specifiers = True if self.specifiers else False
        if self.is_vcs:
            include_extras = False
        if self.is_file_or_url or self.is_vcs:
            include_specifiers = False
        parts = [
            self.req.line_part,
            self.extras_as_pip if include_extras else "",
            self.specifiers if include_specifiers else "",
            self.markers_as_pip if include_markers else "",
        ]
        if as_list:
            # This is used for passing to a subprocess call
            parts = ["".join(parts)]
        if include_hashes:
            hashes = self.get_hashes_as_pip(as_list=as_list)
            if as_list:
                parts.extend(hashes)
            else:
                parts.append(hashes)

        is_local = self.is_file_or_url and self.req and self.req.is_local
        if sources and self.requirement and not (is_local or self.vcs):
            from ..utils import prepare_pip_source_args

            if self.index:
                sources = [s for s in sources if s.get("name") == self.index]
            source_list = prepare_pip_source_args(sources)
            if as_list:
                parts.extend(sources)
            else:
                index_string = " ".join(source_list)
                parts.extend([" ", index_string])
        if as_list:
            return parts
        line = "".join(parts)
        return line

    def get_markers(self):
        # type: () -> Marker
        markers = self.markers
        if markers:
            fake_pkg = PackagingRequirement("fakepkg; {0}".format(markers))
            markers = fake_pkg.marker
        return markers

    def get_specifier(self):
        # type: () -> Union[SpecifierSet, LegacySpecifier]
        try:
            return Specifier(self.specifiers)
        except InvalidSpecifier:
            return LegacySpecifier(self.specifiers)

    def get_version(self):
        return pip_shims.shims.parse_version(self.get_specifier().version)

    def get_requirement(self):
        req_line = self.req.req.line
        if req_line.startswith("-e "):
            _, req_line = req_line.split(" ", 1)
        req = init_requirement(self.name)
        req.line = req_line
        req.specifier = SpecifierSet(self.specifiers if self.specifiers else "")
        if self.is_vcs or self.is_file_or_url:
            req.url = getattr(self.req.req, "url", self.req.link.url_without_fragment)
        req.marker = self.get_markers()
        req.extras = set(self.extras) if self.extras else set()
        return req

    @property
    def constraint_line(self):
        return self.as_line()

    @property
    def is_direct_url(self):
        return self.is_file_or_url and self.req.is_direct_url or (
            self.line_instance.is_direct_url or self.req.parsed_line.is_direct_url
        )

    def as_pipfile(self):
        good_keys = (
            "hashes",
            "extras",
            "markers",
            "editable",
            "version",
            "index",
        ) + VCS_LIST
        req_dict = {
            k: v
            for k, v in attr.asdict(self, recurse=False, filter=filter_none).items()
            if k in good_keys
        }
        name = self.name
        if "markers" in req_dict and req_dict["markers"]:
            req_dict["markers"] = req_dict["markers"].replace('"', "'")
        if not self.req.name:
            name_carriers = (self.req, self, self.line_instance, self.req.parsed_line)
            name_options = [
                getattr(carrier, "name", None)
                for carrier in name_carriers if carrier is not None
            ]
            req_name = next(iter(n for n in name_options if n is not None), None)
            self.req.name = req_name
        req_name, dict_from_subreq = self.req.pipfile_part.popitem()
        base_dict = {
            k: v for k, v in dict_from_subreq.items()
            if k not in ["req", "link", "_setup_info"]
        }
        base_dict.update(req_dict)
        conflicting_keys = ("file", "path", "uri")
        if "file" in base_dict and any(k in base_dict for k in conflicting_keys[1:]):
            conflicts = [k for k in (conflicting_keys[1:],) if k in base_dict]
            for k in conflicts:
                base_dict.pop(k)
        if "hashes" in base_dict:
            _hashes = base_dict.pop("hashes")
            hashes = []
            for _hash in _hashes:
                try:
                    hashes.append(_hash.as_line())
                except AttributeError:
                    hashes.append(_hash)
            base_dict["hashes"] = sorted(hashes)
        if "extras" in base_dict:
            base_dict["extras"] = list(base_dict["extras"])
        if len(base_dict.keys()) == 1 and "version" in base_dict:
            base_dict = base_dict.get("version")
        return {name: base_dict}

    def as_ireq(self):
        if self.line_instance and self.line_instance.ireq:
            return self.line_instance.ireq
        elif getattr(self.req, "_parsed_line", None) and self.req._parsed_line.ireq:
            return self.req._parsed_line.ireq
        kwargs = {
            "include_hashes": False,
        }
        if (self.is_file_or_url and self.req.is_local) or self.is_vcs:
            kwargs["include_markers"] = False
        ireq_line = self.as_line(**kwargs)
        ireq = Line(ireq_line).ireq
        if not getattr(ireq, "req", None):
            ireq.req = self.req.req
            if (self.is_file_or_url and self.req.is_local) or self.is_vcs:
                if getattr(ireq, "req", None) and getattr(ireq.req, "marker", None):
                    ireq.req.marker = None
        else:
            ireq.req.extras = self.req.req.extras
            if not ((self.is_file_or_url and self.req.is_local) or self.is_vcs):
                ireq.req.marker = self.req.req.marker
        return ireq

    @property
    def pipfile_entry(self):
        return self.as_pipfile().copy().popitem()

    @property
    def ireq(self):
        return self.as_ireq()

    def get_dependencies(self, sources=None):
        """Retrieve the dependencies of the current requirement.

        Retrieves dependencies of the current requirement.  This only works on pinned
        requirements.

        :param sources: Pipfile-formatted sources, defaults to None
        :param sources: list[dict], optional
        :return: A set of requirement strings of the dependencies of this requirement.
        :rtype: set(str)
        """

        from .dependencies import get_dependencies

        if not sources:
            sources = [
                {"name": "pypi", "url": "https://pypi.org/simple", "verify_ssl": True}
            ]
        return get_dependencies(self.as_ireq(), sources=sources)

    def get_abstract_dependencies(self, sources=None):
        """Retrieve the abstract dependencies of this requirement.

        Returns the abstract dependencies of the current requirement in order to resolve.

        :param sources: A list of sources (pipfile format), defaults to None
        :param sources: list, optional
        :return: A list of abstract (unpinned) dependencies
        :rtype: list[ :class:`~requirementslib.models.dependency.AbstractDependency` ]
        """

        from .dependencies import (
            AbstractDependency,
            get_dependencies,
            get_abstract_dependencies,
        )

        if not self.abstract_dep:
            parent = getattr(self, "parent", None)
            self.abstract_dep = AbstractDependency.from_requirement(self, parent=parent)
        if not sources:
            sources = [
                {"url": "https://pypi.org/simple", "name": "pypi", "verify_ssl": True}
            ]
        if is_pinned_requirement(self.ireq):
            deps = self.get_dependencies()
        else:
            ireq = sorted(self.find_all_matches(), key=lambda k: k.version)
            deps = get_dependencies(ireq.pop(), sources=sources)
        return get_abstract_dependencies(
            deps, sources=sources, parent=self.abstract_dep
        )

    def find_all_matches(self, sources=None, finder=None):
        """Find all matching candidates for the current requirement.

        Consults a finder to find all matching candidates.

        :param sources: Pipfile-formatted sources, defaults to None
        :param sources: list[dict], optional
        :return: A list of Installation Candidates
        :rtype: list[ :class:`~pip._internal.index.InstallationCandidate` ]
        """

        from .dependencies import get_finder, find_all_matches

        if not finder:
            finder = get_finder(sources=sources)
        return find_all_matches(finder, self.as_ireq())

    def run_requires(self, sources=None, finder=None):
        if self.req and self.req.setup_info is not None:
            info_dict = self.req.setup_info.as_dict()
        elif self.line_instance and self.line_instance.setup_info is not None:
            info_dict = self.line_instance.setup_info.as_dict()
        else:
            from .setup_info import SetupInfo
            if not finder:
                from .dependencies import get_finder
                finder = get_finder(sources=sources)
            info = SetupInfo.from_requirement(self, finder=finder)
            if info is None:
                return {}
            info_dict = info.get_info()
            if self.req and not self.req.setup_info:
                self.req._setup_info = info
        if self.req._has_hashed_name and info_dict.get("name"):
            self.req.name = self.name = info_dict["name"]
            if self.req.req.name != info_dict["name"]:
                self.req.req.name = info_dict["name"]
        return info_dict

    def merge_markers(self, markers):
        if not isinstance(markers, Marker):
            markers = Marker(markers)
        _markers = set(Marker(self.ireq.markers)) if self.ireq.markers else set(markers)
        _markers.add(markers)
        new_markers = Marker(" or ".join([str(m) for m in sorted(_markers)]))
        self.markers = str(new_markers)
        self.req.req.marker = new_markers


def file_req_from_parsed_line(parsed_line):
    # type: (Line) -> FileRequirement
    path = parsed_line.relpath if parsed_line.relpath else parsed_line.path
    pyproject_requires = ()  # type: Tuple[Text]
    if parsed_line.pyproject_requires is not None:
        pyproject_requires = tuple(parsed_line.pyproject_requires)
    return FileRequirement(
        setup_path=parsed_line.setup_py,
        path=path,
        editable=parsed_line.editable,
        extras=parsed_line.extras,
        uri_scheme=parsed_line.preferred_scheme,
        link=parsed_line.link,
        uri=parsed_line.uri,
        pyproject_requires=pyproject_requires,
        pyproject_backend=parsed_line.pyproject_backend,
        pyproject_path=Path(parsed_line.pyproject_toml) if parsed_line.pyproject_toml else None,
        parsed_line=parsed_line,
        name=parsed_line.name,
        req=parsed_line.requirement
    )


def vcs_req_from_parsed_line(parsed_line):
    # type: (Line) -> VCSRequirement
    line = "{0}".format(parsed_line.line)
    if parsed_line.editable:
        line = "-e {0}".format(line)
    if parsed_line.url is not None:
        link = create_link(build_vcs_uri(
            vcs=parsed_line.vcs,
            uri=parsed_line.url,
            name=parsed_line.name,
            ref=parsed_line.ref,
            subdirectory=parsed_line.subdirectory,
            extras=list(parsed_line.extras)
        ))
    else:
        link = parsed_line.link
    pyproject_requires = ()  # type: Tuple[Text]
    if parsed_line.pyproject_requires is not None:
        pyproject_requires = tuple(parsed_line.pyproject_requires)
    return VCSRequirement(
        setup_path=parsed_line.setup_py,
        path=parsed_line.path,
        editable=parsed_line.editable,
        vcs=parsed_line.vcs,
        ref=parsed_line.ref,
        subdirectory=parsed_line.subdirectory,
        extras=parsed_line.extras,
        uri_scheme=parsed_line.preferred_scheme,
        link=link,
        uri=parsed_line.uri,
        pyproject_requires=pyproject_requires,
        pyproject_backend=parsed_line.pyproject_backend,
        pyproject_path=Path(parsed_line.pyproject_toml) if parsed_line.pyproject_toml else None,
        parsed_line=parsed_line,
        name=parsed_line.name,
        req=parsed_line.requirement,
        base_line=line,
    )


def named_req_from_parsed_line(parsed_line):
    # type: (Line) -> NamedRequirement
    return NamedRequirement(
        name=parsed_line.name,
        version=parsed_line.specifier,
        req=parsed_line.requirement,
        extras=parsed_line.extras,
        editable=parsed_line.editable,
        parsed_line=parsed_line
    )

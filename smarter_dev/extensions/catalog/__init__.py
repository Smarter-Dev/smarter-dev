"""The extension catalog: one subpackage per first-party extension.

Each subpackage exports a module-level ``MANIFEST`` (an
:class:`~smarter_dev.extensions.schema.ExtensionManifest`) and ships its handler
script templates as sibling ``*.monty`` files. The registry
(:mod:`smarter_dev.extensions.registry`) discovers subpackages here by
iteration — adding an extension is adding a directory, nothing else.
"""

from __future__ import annotations

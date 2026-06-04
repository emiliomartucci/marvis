"""Packaged Marvis reference guides loaded at runtime by `marvis guide`.

The ``__init__.py`` makes this a regular package so
``importlib.resources.files("core.cli.guides")`` resolves the bundled markdown
identically from a source checkout and from an installed wheel.
"""

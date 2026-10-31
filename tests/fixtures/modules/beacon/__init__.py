"""Beacon: an original example of an external module package.

A keeper shows one of three lamp colours, chosen by a die. The package has a
relative helper (`signals`), JSON data (`signals.json`), and its own
provider for `dice.roll.v1`, `beacon.steady`, which the manifest may select
instead of `builtin.dice`. Tests load it with tesserae.external.
"""

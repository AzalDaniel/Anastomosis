"""Destination packs — see :mod:`.base` for the upload contract.

A destination pack implements the protocols in :mod:`.base` to teach the
browser delivery engine how to file one reconstructed chart into one
foreign EHR (e.g. ``destinations/tebra``); this package ships the contract
and the data types the engine drives, and re-exports nothing: each name
comes from the module that defines it (75)."""

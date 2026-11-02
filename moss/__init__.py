"""MOSS: Wikipedia and community pages over MeshCore radios."""

__version__ = "0.6.0"
# Bumped when a Seed Vault learns a new kind of request, so readers can tell
# "your vault is older" apart from "you asked for something odd".
#   1  search, article            2  community pages, uploads
#   3  combined search, vault specs, spore prints
PROTOCOL = 3

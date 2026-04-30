"""Integrated semantic indexer for Confluence pages.

Imports are lazy — the indexer subpackage can be imported safely even
when sqlite-vec or numpy are not installed. Actual initialization
happens when init_db() / init_embedder() are called.
"""

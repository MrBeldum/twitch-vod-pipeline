"""Twitch VOD -> Premiere pipeline.

Records live streams in chunks, publishes Premiere-ready masters and proxies, and
emits a word-timed transcript plus an editor report for each chunk.
"""

__version__ = "1.0.0"

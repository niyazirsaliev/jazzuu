from __future__ import annotations

import re

from .contracts import TokenCounter


_SENTENCE = re.compile(r"(?<=[.!?…])\s+|\n+")


class DeterministicChunker:
    version = "sentences-v1"

    def __init__(self, token_counter: TokenCounter, max_tokens: int = 384, overlap_tokens: int = 32):
        self.counter = token_counter
        self.max_tokens = int(max_tokens)
        self.overlap_tokens = int(overlap_tokens)
        if self.max_tokens < 1 or not 0 <= self.overlap_tokens < self.max_tokens:
            raise ValueError("invalid chunk bounds")

    def _split_oversized_token(self, token: str) -> list[str]:
        output = []
        current = ""
        for character in token:
            candidate = current + character
            if current and self.counter.count(candidate) > self.max_tokens:
                output.append(current)
                current = character
            else:
                current = candidate
            if self.counter.count(current) > self.max_tokens:
                raise ValueError("one character exceeds chunk limit")
        if current:
            output.append(current)
        return output

    def _start_with_overlap(self, previous: str, word: str) -> str:
        overlap = previous.split()[-self.overlap_tokens :] if self.overlap_tokens else []
        candidate = " ".join([*overlap, word])
        while overlap and self.counter.count(candidate) > self.max_tokens:
            overlap.pop(0)
            candidate = " ".join([*overlap, word])
        if self.counter.count(candidate) > self.max_tokens:
            raise ValueError("chunk token exceeds limit")
        return candidate

    def _pieces(self, text: str) -> list[str]:
        sentences = [" ".join(value.split()) for value in _SENTENCE.split(text or "")]
        output = []
        for sentence in filter(None, sentences):
            if self.counter.count(sentence) <= self.max_tokens:
                output.append(sentence)
                continue
            words = []
            for word in sentence.split():
                if self.counter.count(word) > self.max_tokens:
                    words.extend(self._split_oversized_token(word))
                else:
                    words.append(word)
            current = []
            for word in words:
                candidate = " ".join([*current, word])
                if current and self.counter.count(candidate) > self.max_tokens:
                    output.append(" ".join(current))
                    overlap = current[-self.overlap_tokens :] if self.overlap_tokens else []
                    current = [*overlap, word]
                    while self.counter.count(" ".join(current)) > self.max_tokens and len(current) > 1:
                        current.pop(0)
                else:
                    current.append(word)
            if current:
                output.append(" ".join(current))
        return output

    def chunk(self, text: str) -> list[str]:
        pieces = self._pieces(text)
        chunks: list[str] = []
        current = ""
        for piece in pieces:
            candidate = f"{current} {piece}".strip()
            if current and self.counter.count(candidate) > self.max_tokens:
                chunks.append(current)
                current = self._start_with_overlap(current, piece.split()[0])
                for word in piece.split()[1:]:
                    candidate = f"{current} {word}".strip()
                    if self.counter.count(candidate) > self.max_tokens:
                        chunks.append(current)
                        current = self._start_with_overlap(current, word)
                    else:
                        current = candidate
            else:
                current = candidate
        if current:
            chunks.append(current)
        if any(self.counter.count(value) > self.max_tokens for value in chunks):
            raise ValueError("chunk exceeds token limit")
        return chunks

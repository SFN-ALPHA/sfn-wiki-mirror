"""Sparse lexical encoding for the keyword half of hybrid retrieval.

`index` encodes every passage with it at build
time and `search` encodes every query with it, so the stored and the query vectors always come
from the same function.

The built keyword index depends on this module's exact behaviour: the tokenizer, the stemmer, the
hash and ``k1`` together decide every stored vector, so changing any of them silently breaks
search over what is already built until a keyword re-index. See docs/DESIGN.md section 12.7.

Dense embeddings are bad at exactly the queries an operator asks most: a hostname, an
error code, a model name. "gemma3:12b" means nothing to a semantic embedder and
everything to a lexical index, which is why hybrid retrieval beats either channel alone
and why reciprocal rank fusion (RRF) of the two is the standard way to combine them.

**How it works.** Tokenize, hash each term into a fixed index space, and weight by BM25:
term-frequency saturation on the document side, inverse document frequency on the query
side. Their dot product is the BM25 score, which is why the two halves can live on
opposite sides of the index.

**Why idf is not optional.** Without it, a document matching one common word scores the
same as one matching a rare identifier - so "how does photosynthesis work in succulents"
retrieves a note about "standard reasoning *work*" and an unanswerable question is
called covered. That is not a ranking nuisance; it is the "nothing relevant here" case
failing, which is the difference between an assistant that admits ignorance and one that
confabulates.

**Qdrant owns the idf, not this module.** This encoder used to count document
frequencies in process memory. That state was lost on every restart (so every deploy
reset every term to idf 1.0 until new ingests rebuilt a tiny, unrepresentative corpus),
never decremented on delete or supersede, and double-counted a re-ingest. The sparse
vector is now configured with Qdrant's ``Modifier.IDF`` (Qdrant 1.10 and later): the
collection computes ``ln((N - df + 0.5) / (df + 0.5) + 1)`` from the points it holds, at
query time, and multiplies it into the query's term weights. That is the formula this
module used, so scores keep their meaning; the difference is that the statistic lives
beside the data it describes and follows every upsert, overwrite and delete. There is no
client state to lose, so there is nothing to rebuild at startup and nothing to keep
consistent. Both halves therefore send plain term-frequency weights, and a collection
**without** the modifier scores every term as equally informative - which is why the mirror's
index creates its collection with it.

**No document-length normalisation (BM25 b = 0), on purpose.** Classic BM25 divides each
term's saturation by a length ratio that needs the corpus's average document length -
another statistic to store and keep right, which is the failure this change removes.
Chunks are bounded by the chunker, so the skew is bounded too, and a term weighs the same
in a one-line note as in a long chunk. If measurement ever argues for it, it can be done
statelessly against the chunker's fixed target length and applied by a re-index.

Hashing rather than a learned vocabulary keeps this deterministic and dependency-free:
the same text always produces the same vector, on any machine, with no model download.

See docs/DESIGN.md section 12.7.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["DEFAULT_DIMENSIONS", "SparseEncoder", "tokenize"]

#: Index space for hashed terms. Large enough that collisions are rare at our corpus
#: size, small enough that vectors stay cheap.
DEFAULT_DIMENSIONS = 2**18

#: BM25's term-frequency saturation constant. Controls how fast repeated occurrences
#: stop adding weight - the reason a document mentioning a term twenty times is not
#: twenty times more relevant than one mentioning it twice.
_K1 = 1.5

#: Words too common to discriminate. A short list on purpose: aggressive stopword
#: removal hurts exactly the short technical queries this channel exists to serve.
_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "have",
        "he", "in", "is", "it", "its", "of", "on", "or", "she", "that", "the", "they",
        "this", "to", "was", "were", "will", "with",
    }
)

#: Words, numbers, and the punctuation that appears inside identifiers - a tokenizer
#: that splits "gemma3:12b" into three tokens has destroyed the query.
_TOKEN = re.compile(r"[A-Za-z0-9]+(?:[._:\-/][A-Za-z0-9]+)*")

#: Separators inside a compound identifier, used to emit its parts alongside the whole.
_SEPARATOR = re.compile(r"[._:\-/]")

#: Shortest stem a suffix rule may leave behind. Guards against turning short words into
#: fragments that collide with unrelated terms.
_MIN_STEM = 3


def tokenize(text: str) -> list[str]:
    """Split text into lowercase, lightly stemmed terms.

    Two behaviors beyond splitting, both of which are the difference between a lexical
    channel that works and one that only matches text quoted back at it:

    **Identifiers emit their parts as well as the whole.** ``x-request-id`` indexes as
    itself *and* as ``x``, ``request``, ``id``, so an operator asking "how are requests
    correlated" reaches a note that only ever spells it ``X-Request-ID``. Keeping the
    whole token too means an exact identifier search still gets an exact hit.

    **Light suffix stripping.** ``calls`` and ``call``, ``listens`` and ``listen``,
    ``streamed`` and ``stream`` are the same term to anyone asking a question, and a
    channel that disagrees fails on the most natural phrasings. Deliberately conservative
    - three rules, no vowel analysis - because an aggressive stemmer collides unrelated
    words and that failure is much harder to see than a miss.
    """
    terms: list[str] = []
    for match in _TOKEN.finditer(text):
        whole = match.group(0).lower()
        for part in _expand(whole):
            if part not in _STOPWORDS:
                terms.append(_stem(part))
    return terms


def _expand(token: str) -> list[str]:
    """A token plus its constituent parts, when it is a compound identifier."""
    parts = _SEPARATOR.split(token)
    if len(parts) == 1:
        return [token]
    return [token, *(part for part in parts if part)]


def _stem(term: str) -> str:
    """Strip the three suffixes that separate a question's wording from a note's."""
    if len(term) > _MIN_STEM + 1 and term.endswith("ies"):
        return term[:-3] + "y"
    if len(term) > _MIN_STEM and term.endswith("ing"):
        return term[:-3]
    if len(term) > _MIN_STEM and term.endswith("ed"):
        return term[:-2]
    if len(term) > _MIN_STEM and term.endswith("s") and not term.endswith(("ss", "us", "is")):
        return term[:-1]
    return term


@dataclass(frozen=True)
class SparseEncoder:
    """Turns text into a sparse ``{index: weight}`` vector. Stateless: Qdrant owns the idf.

    Frozen on purpose: it used to hold corpus statistics, which is exactly what made it
    something that could be lost, and it holds only configuration now. One instance is
    still shared by ingestion and retrieval, but only so both hash with the same
    ``dimensions`` and saturate with the same ``k1`` - the stored and the query vectors
    must agree on those or their dot product means nothing.
    """

    dimensions: int = DEFAULT_DIMENSIONS
    k1: float = _K1

    def encode_document(self, text: str) -> dict[int, float]:
        """Encode text for storage: term-frequency saturation only.

        idf belongs to the collection (Qdrant's ``Modifier.IDF``), applied when a query
        runs. Storing it would bake a snapshot of the corpus into every vector, so every
        ingest would silently invalidate the weighting of everything already stored.
        """
        return self._weights(text)

    def encode_query(self, text: str) -> dict[int, float]:
        """Encode a query: the same saturated term weights, with no idf applied here.

        Qdrant multiplies each weight by the term's idf from the live collection, so the
        dot product against a stored document vector is the BM25 score. A term appearing
        in most documents contributes almost nothing, which is precisely what stops a
        common word from making an unanswerable question look covered. Applying idf here
        as well would count it twice.
        """
        return self._weights(text)

    def _weights(self, text: str) -> dict[int, float]:
        """BM25 term-frequency saturation, ``tf * (k1 + 1) / (tf + k1)``, per term index."""
        return {
            index: (count * (self.k1 + 1.0)) / (count + self.k1)
            for index, count in self._counts(text).items()
        }

    def _counts(self, text: str) -> dict[int, int]:
        """Term index to occurrence count within one text."""
        counts: dict[int, int] = {}
        for token in tokenize(text):
            index = self._index_of(token)
            counts[index] = counts.get(index, 0) + 1
        return counts

    def _index_of(self, token: str) -> int:
        """Map a term to its index.

        Python's ``hash`` is salted per process, so it would produce a different vector
        on every restart - stored vectors and query vectors would stop agreeing. A
        stable arithmetic hash is required, not merely preferable.
        """
        digest = 0
        for char in token:
            digest = (digest * 31 + ord(char)) & 0xFFFFFFFF
        return digest % self.dimensions

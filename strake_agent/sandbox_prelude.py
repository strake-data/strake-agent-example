import re
import json
from collections import defaultdict, Counter


class _CachedTable:
    def __init__(self, rows):
        self._rows = list(rows)

    def to_pylist(self):
        return list(self._rows)

    def to_pydict(self):
        if not self._rows:
            return {}
        return {k: [r[k] for r in self._rows] for k in self._rows[0].keys()}

    def to_pandas(self):
        import pandas as pd

        return pd.DataFrame(self._rows)

    def to_arrow(self):
        import pyarrow as pa

        return pa.Table.from_pylist(self._rows)

    def to_string(self):
        try:
            return self.to_pandas().to_string()
        except Exception:
            return repr(self)

    def to_dict(self):
        return self.to_pydict()

    def to_json(self):
        return json.dumps(self._rows)

    @property
    def columns(self):
        return self.column_names

    def __getitem__(self, key):
        if isinstance(key, str):
            return self.column_values(key)
        elif isinstance(key, int):
            return self._rows[key]
        # Slice, list/Series of keys/booleans -> delegate to pandas DataFrame
        import pandas as pd

        df = self.to_pandas()
        res = df[key]
        if isinstance(res, pd.DataFrame):
            return _CachedTable(res.to_dict(orient="records"))
        elif hasattr(res, "tolist"):
            return res.tolist()
        return res

    def __setitem__(self, key, value):
        if isinstance(key, str):
            if (
                hasattr(value, "__len__")
                and not isinstance(value, (str, dict))
                and len(value) == len(self._rows)
            ):
                value_list = list(value)
                for i, r in enumerate(self._rows):
                    r[key] = value_list[i]
            else:
                for r in self._rows:
                    r[key] = value
        elif isinstance(key, int):
            self._rows[key] = value

    def __getattr__(self, name):
        df = self.to_pandas()
        attr = getattr(df, name)
        if callable(attr):

            def _wrapper(*args, **kwargs):
                import pandas as pd

                res = attr(*args, **kwargs)
                if isinstance(res, pd.DataFrame):
                    return _CachedTable(res.to_dict(orient="records"))
                return res

            return _wrapper
        return attr

    def __iter__(self):
        return iter(self._rows)

    def __len__(self):
        return len(self._rows)

    @property
    def num_rows(self):
        return len(self._rows)

    @property
    def empty(self):
        return len(self._rows) == 0

    @property
    def column_names(self):
        if not self._rows:
            return []
        return list(self._rows[0].keys())

    @property
    def shape(self):
        return (len(self._rows), len(self.column_names))

    @property
    def schema(self):
        class _MockSchema:
            def __init__(self, names):
                self.names = names

        cols = list(self._rows[0].keys()) if self._rows else []
        return _MockSchema(cols)

    def column_values(self, name):
        return [r[name] for r in self._rows if name in r]

    def column(self, name):
        class _MockColumn:
            def __init__(self, values):
                self._values = values

            def to_pylist(self):
                return list(self._values)

        return _MockColumn([r[name] for r in self._rows if name in r])

    def __repr__(self):
        if not self._rows:
            return "Table(0 rows)"
        cols = list(self._rows[0].keys())
        return f"Table({len(self._rows)} rows × {len(cols)} cols: {', '.join(cols)})"

    def iter_batches(self, batch_size=1024):
        class _CachedBatch:
            def __init__(self, batch_rows):
                self._batch_rows = batch_rows

            def to_pylist(self):
                return list(self._batch_rows)

            @property
            def num_rows(self):
                return len(self._batch_rows)

            def __len__(self):
                return len(self._batch_rows)

        for i in range(0, len(self._rows), batch_size):
            yield _CachedBatch(self._rows[i : i + batch_size])


class _StrakeProxy:
    """Drop-in strake replacement with query caching and call-count limit."""

    def __init__(self, real):
        self._real = real
        # Load pre-populated cache from globals if present
        pre_populated = globals().get("_PRE_POPULATED_CACHE", {})
        self._cache = dict(pre_populated)
        self._count = 0

    def sql(self, query, *args, **kwargs):
        key = query.strip()
        # Cache hit — free, no count increment
        if key in self._cache:
            rows = self._cache[key]
        else:
            self._count += 1
            if self._count > 20:
                raise RuntimeError(
                    f"SQL call limit reached ({self._count} unique queries). "
                    "You are issuing too many separate queries. "
                    "Fetch the full table once and join/filter in Python using a dict."
                )
            rows = self._real.sql(query, *args, **kwargs).to_pylist()
            self._cache[key] = rows

        return _CachedTable(rows)

    def search(self, *args, **kwargs):
        return self._real.search(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


# This object is injected by the sandbox environment, but we wrap it here.
# Note: In the actual sandbox execution, 'strake' is already in the namespace.
strake = _StrakeProxy(strake)

# Secure Namespace Patching: Prevent `import strake` from overwriting our proxy
try:
    import typing

    sys = typing.sys
    sys.modules["strake"] = strake
except Exception:
    pass

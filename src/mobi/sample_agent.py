"""DatabricksAgent: a focused Databricks-first conversational helper.

This module replaces earlier sample logic with a single `DatabricksAgent`
class intended to be imported and used inside Databricks notebooks or Jobs.

The agent routes natural-language queries to the deployed SQL/Python tools in
`vanhack.mobi_data`: `live_station_status`, `nearby_stations`, and
`recent_trips_by_station`.

Behavior goals:
- Keep changes read-only (no metadata writes) by default.
- Provide clear, concise answers and structured results for downstream tools.
- Be safe to use interactively by analysts and in Agent Bricks as a tool.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from pyspark.sql import SparkSession


class DatabricksAgent:
    """Lightweight agent to route queries to Databricks table functions.

    Example usage (Databricks notebook):

    >>> agent = DatabricksAgent(spark)
    >>> agent.query("Are there bikes available at station 0152?")
    {
      "intent": "live_status",
      "station_id": "0152",
      "answer": "Station 0152: bikes=3, docks=10, renting=True, returning=True",
      "raw": { ... }
    }
    """

    def __init__(self, spark: SparkSession, catalog: str = "vanhack"):
        """Create an agent bound to a SparkSession.

        Args:
            spark: active SparkSession
            catalog: Unity Catalog name to USE (defaults to 'vanhack')
        """
        self.spark = spark
        self.catalog = catalog
        # Ensure we are using the right catalog for resolution
        try:
            self.spark.sql(f"USE CATALOG {self.catalog}")
        except Exception:
            # ignore errors here; callers can still run fully-qualified names
            pass

    @staticmethod
    def _escape_literal(value: str) -> str:
        """Very small helper to escape single quotes for SQL string literals."""
        return value.replace("'", "''")

    @staticmethod
    def _format_missing_function_hint(function_name: str) -> Dict[str, Any]:
        return {
            "error": f"Function '{function_name}' not found in Unity Catalog",
            "hint": (
                "Verify the notebook in 02_tools.ipynb has been run and that "
                "the function exists in `vanhack.mobi_data`."
            ),
        }

    def _function_exists(self, function_name: str) -> bool:
        """Check Unity Catalog for a function with this name in vanhack.mobi_data.

        Returns True if a function with the exact name is present. This helps
        give better errors when signatures don't match.
        """
        try:
            df = self.spark.sql("SHOW FUNCTIONS IN vanhack.mobi_data")
            names = [r.function for r in df.collect() if hasattr(r, "function")]
            return function_name in names
        except Exception:
            # If SHOW FUNCTIONS fails for some reason, return False and let
            # the caller surface the original error.
            return False

    # NOTE: site_search and site-table helpers removed — agent focuses on
    # the core table-valued functions: live_station_status, nearby_stations,
    # and recent_trips_by_station.

    # ---- low-level callers for the deployed tools -----------------
    def _call_live_status(self, station_id: str) -> Optional[Dict[str, Any]]:
        # Try a couple of common invocation styles. Databricks resolves
        # table-valued functions by name+signature, so a mismatch (string vs
        # int) will produce an UnresolvedTableValuedFunction error.
        fn = "live_station_status"
        if not self._function_exists(fn):
            return self._format_missing_function_hint(fn)

        safe_station = self._escape_literal(station_id)
        candidates = []
        # 1) quoted string, fully qualified with backticks
        candidates.append(f"SELECT * FROM TABLE(`vanhack`.`mobi_data`.{fn}('{safe_station}'))")
        # 2) unquoted numeric (try if station_id looks like an int)
        try:
            intval = int(station_id)
            candidates.append(f"SELECT * FROM TABLE(`vanhack`.`mobi_data`.{fn}({intval}))")
        except Exception:
            pass

        last_err = None
        for sql in candidates:
            try:
                df = self.spark.sql(sql)
                rows = [r.asDict() for r in df.collect()]
                return rows[0] if rows else None
            except Exception as e:
                last_err = e

        # If we reach here, all candidates failed. Surface a helpful message
        # including the original error text and advice to inspect the function
        # signature in Databricks.
        msg = str(last_err) if last_err is not None else "unknown error"
        return {
            "error": msg,
            "hint": (
                "Function exists but may not accept a string argument. "
                "In a Databricks notebook run:\n"
                "  spark.sql(\"DESCRIBE FUNCTION EXTENDED vanhack.mobi_data.live_station_status\").show()\n"
                "or:\n"
                "  spark.sql(\"SHOW FUNCTIONS IN vanhack.mobi_data LIKE 'live_station_status'\").show()\n"
                "to inspect signatures. Try calling the function with the correct type "
                "(string vs int)."
            ),
        }

    def _call_nearby(self, lat: float, lon: float, radius_km: float = 1.0) -> List[Dict[str, Any]]:
        fn = "nearby_stations"
        if not self._function_exists(fn):
            return [self._format_missing_function_hint(fn)]

        sql = (
            f"SELECT * FROM TABLE(`vanhack`.`mobi_data`.{fn}({lat:.6f}, {lon:.6f}, {radius_km:.6f}))"
        )
        try:
            df = self.spark.sql(sql)
            return [r.asDict() for r in df.collect()]
        except Exception as e:
            return [{"error": str(e), "hint": "Check function signature with SHOW/DESCRIBE in Databricks."}]

    def _call_recent_trips(self, station_id: str, limit: int = 5) -> List[Dict[str, Any]]:
        fn = "recent_trips_by_station"
        if not self._function_exists(fn):
            return [self._format_missing_function_hint(fn)]

        safe_station = self._escape_literal(station_id)
        candidates = [f"SELECT * FROM TABLE(`vanhack`.`mobi_data`.{fn}('{safe_station}')) LIMIT {int(limit)}"]
        try:
            intval = int(station_id)
            candidates.append(f"SELECT * FROM TABLE(`vanhack`.`mobi_data`.{fn}({intval})) LIMIT {int(limit)}")
        except Exception:
            pass

        last_err = None
        for sql in candidates:
            try:
                df = self.spark.sql(sql)
                return [r.asDict() for r in df.collect()]
            except Exception as e:
                last_err = e

        return [{"error": str(last_err), "hint": "Inspect function signature with DESCRIBE/SHOW in Databricks."}]
    # site_search removed — agent focuses on live status, nearby, and recent trips

    # ---- intent parsing -------------------------------------------
    def _parse_station_id(self, text: str) -> Optional[str]:
        m = re.search(r"station\s*(\d{2,})", text)
        if m:
            return m.group(1)
        return None

    def _parse_coords(self, text: str) -> Optional[Tuple[float, float]]:
        # Find two floats (lat, lon)
        coords = re.findall(r"(-?\d+\.\d+)", text)
        if len(coords) >= 2:
            return float(coords[0]), float(coords[1])
        return None

    def _detect_intent(self, text: str) -> Tuple[str, Dict[str, Any]]:
        t = text.lower().strip()
        # live status
        if any(k in t for k in ("available", "availability", "bikes available", "bikes at station")):
            station = self._parse_station_id(t)
            if station:
                return "live_status", {"station_id": station}

        # recent trips
        if "recent trips" in t or ("recent" in t and "trip" in t):
            station = self._parse_station_id(t)
            if station:
                return "recent_trips", {"station_id": station}

        # nearby
        if any(k in t for k in ("near", "nearby", "closest")):
            coords = self._parse_coords(t)
            if coords:
                return "nearby", {"lat": coords[0], "lon": coords[1], "radius_km": 1.0}

        # docs / faq: no site_search function used — return help instead
        if t.endswith("?") or any(k in t for k in ("how to", "how do i", "what is", "pricing", "fare", "policy")):
            return "help", {}

        # fallback: show help
        return "help", {}

    # ---- public API -----------------------------------------------
    def query(self, message: str) -> Dict[str, Any]:
        """Handle a user message and return a structured response.

        Returns a dict with keys: intent, params, answer (human-friendly), raw (tool output)
        """
        intent, params = self._detect_intent(message)
        if intent == "live_status":
            res = self._call_live_status(params["station_id"])
            if res is None:
                return {"intent": intent, "params": params, "answer": f"No live status found for station {params['station_id']}", "raw": None}
            if "error" in res:
                return {"intent": intent, "params": params, "answer": "Error calling live status", "raw": res}
            txt = (
                f"Station {res.get('station_id')}: bikes={res.get('num_bikes_available')}, "
                f"docks={res.get('num_docks_available')}, renting={res.get('is_renting')}, returning={res.get('is_returning')}"
            )
            return {"intent": intent, "params": params, "answer": txt, "raw": res}

        if intent == "recent_trips":
            rows = self._call_recent_trips(params["station_id"], limit=5)
            if rows and isinstance(rows, list) and "error" in rows[0]:
                return {"intent": intent, "params": params, "answer": "Error fetching recent trips", "raw": rows}
            if not rows:
                return {"intent": intent, "params": params, "answer": f"No recent trips for station {params['station_id']}", "raw": rows}
            lines = [f"{r.get('trip_id')} | dep: {r.get('departure_time')} | dur: {r.get('duration_sec')}s" for r in rows]
            return {"intent": intent, "params": params, "answer": "Recent trips:\n" + "\n".join(lines), "raw": rows}

        if intent == "nearby":
            rows = self._call_nearby(params["lat"], params["lon"], params.get("radius_km", 1.0))
            if rows and isinstance(rows, list) and "error" in rows[0]:
                return {"intent": intent, "params": params, "answer": "Error calling nearby_stations", "raw": rows}
            if not rows:
                return {"intent": intent, "params": params, "answer": "No stations found nearby.", "raw": rows}
            lines = [f"{r.get('station_id')}: {r.get('station_name')} ({round(r.get('distance_km', 0),3)} km)" for r in rows]
            return {"intent": intent, "params": params, "answer": "Nearby stations:\n" + "\n".join(lines), "raw": rows}

        # site_search removed; docs/FAQ questions map to help

        # help
        help_text = (
            "I can help with the following:\n"
            "- Check live station availability: 'Are there bikes available at station 0152?'\n"
            "- Show recent trips for a station: 'Recent trips at station 0152'\n"
            "- Find nearby stations: 'Find stations near 49.2827, -123.1207'\n"
            "- (Docs/FAQ: ask your question; agent will show help if unclear)\n"
        )
        return {"intent": "help", "params": {}, "answer": help_text, "raw": None}


def demo(spark: SparkSession):
    agent = DatabricksAgent(spark)
    queries = [
        "Are there bikes available at station 0152?",
        "Recent trips at station 0152",
        "Find stations near 49.2827, -123.1207",
    ]
    for q in queries:
        print("Q:", q)
        try:
            out = agent.query(q)
            print("A:", out["answer"])
            print("--- raw:", out["raw"])
        except Exception as e:
            print("Error:", e)


if __name__ == "__main__":
    print("Import DatabricksAgent and call agent.query(message) from a Databricks notebook.")

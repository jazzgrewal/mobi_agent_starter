"""Sample agent helpers for Databricks + Agent Bricks integration.

This module provides lightweight wrappers around existing Databricks SQL
functions and the metadata agent. It is intentionally small and designed to
be imported in a Databricks notebook (or job) so you can expose the same
capabilities as Agent Bricks tools.

Usage (Databricks notebook):

from mobi.sample_agent import ChatAgent
agent = ChatAgent(spark)
print(agent.handle_message("Are there bikes available at station 0152?"))

The ChatAgent is rule-based and demonstrates how to route user messages to
the SQL tools already created in the notebooks (recent_trips_by_station,
live_station_status, nearby_stations, site_search) and to the metadata
analyzer.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from pyspark.sql import DataFrame, SparkSession

from mobi import metadata_agent


class ChatAgent:
    """Very small rule-based chat agent that calls Databricks tools.

    This is a starting point for wiring into Agent Bricks. It shows how to
    call SQL functions from Python and return readable responses.
    """

    def __init__(self, spark: SparkSession):
        self.spark = spark

    # Tool wrappers -------------------------------------------------
    def get_recent_trips(self, station_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        sql = f"SELECT * FROM recent_trips_by_station('{station_id}') LIMIT {limit}"
        df = self.spark.sql(sql)
        return [row.asDict() for row in df.collect()]

    def get_live_status(self, station_id: str) -> Optional[Dict[str, Any]]:
        sql = f"SELECT * FROM live_station_status('{station_id}')"
        df = self.spark.sql(sql)
        rows = [r.asDict() for r in df.collect()]
        return rows[0] if rows else None

    def find_nearby(self, lat: float, lon: float, radius_km: float = 1.0) -> List[Dict[str, Any]]:
        sql = f"SELECT * FROM nearby_stations({lat}, {lon}, {radius_km})"
        df = self.spark.sql(sql)
        return [r.asDict() for r in df.collect()]

    def search_docs(self, query: str, num_results: int = 3) -> List[Dict[str, Any]]:
        # Call the site_search function produced by notebook 03_vector_search
        sql = f"SELECT * FROM site_search('{query}') LIMIT {num_results}"
        df = self.spark.sql(sql)
        return [r.asDict() for r in df.collect()]

    def analyze_table(self, catalog: str, schema: str, table: str, sample_limit: int = 2000) -> List[Dict[str, Any]]:
        # metadata_agent.analyze_and_update_table(..., dry_run=True) returns suggestions
        return metadata_agent.analyze_and_update_table(self.spark, catalog, schema, table, sample_limit=sample_limit, dry_run=True)

    def apply_comments(self, catalog: str, schema: str, table: str, columns: Optional[List[str]] = None, suggestions: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Apply comments for given columns.

        If `columns` is None, will apply for all columns using metadata_agent's apply path.
        If `columns` is provided, it expects `suggestions` mapping column->comment.
        """
        full = f"`{catalog}`.`{schema}`.`{table}`"
        results = {}
        if columns is None:
            # Apply all using metadata_agent (it will attempt to apply all)
            try:
                metadata_agent.analyze_and_update_table(self.spark, catalog, schema, table, dry_run=False)
                return {"status": "ok", "applied_all": True}
            except Exception as e:
                return {"status": "error", "error": str(e)}

        # Apply subset
        if suggestions is None:
            raise ValueError("suggestions mapping required when columns list is provided")

        for col in columns:
            comment = suggestions.get(col)
            if comment is None:
                results[col] = {"status": "skipped", "reason": "no suggestion provided"}
                continue
            safe = comment.replace("'", "\\'")
            sql = f"COMMENT ON COLUMN {full}.{col} IS '{safe}'"
            try:
                self.spark.sql(sql)
                results[col] = {"status": "ok", "sql": sql}
            except Exception as e:
                results[col] = {"status": "error", "error": str(e), "sql": sql}

        return results

    # Simple message routing ---------------------------------------
    def handle_message(self, message: str) -> str:
        """Route a user message to the most-likely tool and return a plain-text reply."""
        text = message.lower().strip()

        # Station availability
        m = re.search(r"station\s*(\d{3,})", text)
        if "available" in text or "availability" in text or m:
            station = None
            if m:
                station = m.group(1)
            # fallback: try to find a station id pattern
            if station:
                status = self.get_live_status(station)
                if not status:
                    return f"No live status found for station {station}."
                return (
                    f"Station {status.get('station_id')}: bikes={status.get('num_bikes_available')}, "
                    f"docks={status.get('num_docks_available')}, renting={status.get('is_renting')}, returning={status.get('is_returning')}"
                )

        # Nearby station by coordinates
        if "near" in text or "nearby" in text or "closest" in text:
            coords = re.findall(r"(-?\d+\.\d+)", text)
            if len(coords) >= 2:
                lat, lon = float(coords[0]), float(coords[1])
                rows = self.find_nearby(lat, lon, radius_km=1.0)
                if not rows:
                    return "No stations found within 1 km."
                out = [f"{r['station_id']}: {r['station_name']} ({round(r['distance_km'],3)} km)" for r in rows]
                return "Nearby stations:\n" + "\n".join(out)
            return "To find nearby stations, provide coordinates (lat, lon)."

        # Recent trips by station
        if "recent trips" in text or "recent" in text and "trip" in text:
            m = re.search(r"station\s*(\d{3,})", text)
            if not m:
                return "Please specify a station id to list recent trips (e.g., 'recent trips at station 0152')."
            station = m.group(1)
            rows = self.get_recent_trips(station, limit=5)
            if not rows:
                return f"No recent trips found for station {station}."
            lines = []
            for r in rows:
                dep = r.get('departure_time')
                ret = r.get('return_time')
                dur = r.get('duration_sec')
                lines.append(f"{r.get('trip_id')} | dep: {dep} | ret: {ret} | dur: {dur}")
            return "Recent trips:\n" + "\n".join(lines)

        # Documentation search
        if any(k in text for k in ("what is mobi", "how to rent", "pricing", "fare", "policy")) or text.endswith("?"):
            rows = self.search_docs(message, num_results=3)
            if not rows:
                return "No documentation matches found."
            resp = []
            for r in rows:
                resp.append(f"{r.get('title')} — {r.get('value')[:240]}...")
            return "Top docs:\n" + "\n\n".join(resp)

        # Fallback: offer help
        return (
            "I can: check station availability, list recent trips, find nearby stations by coordinates, or search Mobi docs."
        )


def demo(spark: SparkSession):
    agent = ChatAgent(spark)
    examples = [
        "Are there bikes available at station 0152?",
        "Find stations near 49.2827, -123.1207",
        "Show recent trips at station 0152",
        "How do I rent a bike?",
    ]
    for q in examples:
        print("Q:", q)
        try:
            print("A:", agent.handle_message(q))
        except Exception as e:
            print("Error handling message:", e)


if __name__ == "__main__":
    print("This module is intended to be used inside Databricks notebooks. Import ChatAgent and call handle_message().")

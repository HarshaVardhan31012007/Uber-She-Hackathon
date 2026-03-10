"""
Driver Pulse — Signal Processing Pipeline
==========================================
Modular pipeline that ingests raw sensor and trip data,
runs detection logic for harsh events, and computes earnings velocity.

Usage:
    python driver_pulse_pipeline.py

Outputs (written to ./outputs/):
    flagged_moments.csv        — detected stress/conflict events
    trip_summaries.csv         — per-trip report card
    driver_goals_enriched.csv  — goal forecasts with velocity

Architecture:
    ingest()  →  extract_motion_features()  →  detect_motion_events()
             →  detect_audio_events()
             →  fuse_signals()              →  build_trip_summaries()
             →  forecast_earnings_goals()
             →  save_outputs()
"""

import os
import json
import pandas as pd  # type: ignore
import numpy as np   # type: ignore
from datetime import datetime, timedelta

# ─── CONFIGURATION ─────────────────────────────────────────────────────────────
# Paths
DATA_DIR   = "./data"   # adjust to your local path
OUTPUT_DIR = "./outputs"

# Motion thresholds (horizontal g-force magnitude in m/s²)
HARSH_BRAKE_THRESHOLD    = 2.5   # ISA-referenced harsh event
MODERATE_BRAKE_THRESHOLD = 1.8   # Elevated but sub-harsh

# Audio thresholds
AUDIO_HIGH_DB          = 70     # dB level considered loud
AUDIO_SUSTAINED_SEC    = 20     # seconds sustained before flagging
AUDIO_HIGH_CONFIDENCE_DB = 85   # dB for high-confidence spike (no sustain needed)

# Signal fusion
COMBO_WINDOW_SEC  = 120   # seconds within which motion+audio are considered co-incident
COMBO_WEIGHT_MOTION = 0.55  # motion signal is physics-based, weighted higher
COMBO_WEIGHT_AUDIO  = 0.45

# Severity thresholds (combined score)
HIGH_SEVERITY_THRESHOLD   = 0.75
MEDIUM_SEVERITY_THRESHOLD = 0.55


# ─── STEP 1: DATA INGESTION ────────────────────────────────────────────────────

def ingest(data_dir: str) -> dict:
    """Load all raw CSV files and return as a dictionary of DataFrames."""
    paths = {
        "trips":   f"{data_dir}/trips/trips.csv",
        "drivers": f"{data_dir}/drivers/drivers.csv",
        "acc":     f"{data_dir}/sensor_data/accelerometer_data.csv",
        "audio":   f"{data_dir}/sensor_data/audio_intensity_data.csv",
        "goals":   f"{data_dir}/earnings/driver_goals.csv",
        "vel_log": f"{data_dir}/earnings/earnings_velocity_log.csv",
    }
    data = {}
    for key, path in paths.items():
        data[key] = pd.read_csv(path)
        print(f"  Loaded {key}: {len(data[key])} rows from {path}")
    return data


# ─── STEP 2: MOTION FEATURE EXTRACTION ────────────────────────────────────────

def extract_motion_features(acc_df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute horizontal g-force magnitude from accelerometer X/Y axes.
    We use only horizontal (XY) components — Z-axis captures gravity (~9.8)
    and vertical road vibration, which is not informative for driving events.
    """
    acc_df = acc_df.copy()
    acc_df["horizontal_magnitude"] = np.sqrt(
        acc_df["accel_x"] ** 2 + acc_df["accel_y"] ** 2
    )
    return acc_df


# ─── STEP 3: MOTION EVENT DETECTION ───────────────────────────────────────────

def detect_motion_events(acc_df: pd.DataFrame) -> pd.DataFrame:
    """
    Classify accelerometer readings as harsh_braking, moderate_brake, or normal.

    Thresholds:
        >= 2.5 m/s²  → harsh_braking   (exceeds typical urban deceleration)
        >= 1.8 m/s²  → moderate_brake  (elevated, not extreme)
        <  1.8 m/s²  → normal (filtered out)

    Scoring:
        motion_score = min(magnitude / normaliser, cap)
        This maps raw magnitude onto [0, 1] for fusion with audio scores.
    """
    def classify(row):
        m = row["horizontal_magnitude"]
        if m >= HARSH_BRAKE_THRESHOLD:
            return "harsh_braking", round(min(m / 7.0, 0.95), 2)
        elif m >= MODERATE_BRAKE_THRESHOLD:
            return "moderate_brake", round(min(m / 5.0, 0.75), 2)
        return None, 0.0

    acc_df = acc_df.copy()
    acc_df["motion_label"], acc_df["motion_score"] = zip(*acc_df.apply(classify, axis=1))
    events = acc_df[acc_df["motion_label"].notna()].copy()
    print(f"  Motion events: {len(events)} ({events['motion_label'].value_counts().to_dict()})")
    return events


# ─── STEP 4: AUDIO EVENT DETECTION ────────────────────────────────────────────

def detect_audio_events(audio_df: pd.DataFrame) -> pd.DataFrame:
    """
    Classify audio readings as audio_spike or normal.

    Privacy-safe: Only uses aggregated dB levels and pre-bucketed labels.
    No audio content is ever accessed or stored.

    Detection rules:
        - classification == "argument" → high-confidence spike
        - dB >= 85 AND sustained >= 20s → high-confidence sustained noise
        - (very_loud OR loud) AND dB >= 70 → moderate-confidence spike
    """
    def classify(row):
        db = row["audio_level_db"]
        sustained = row["sustained_duration_sec"]
        classification = row["audio_classification"]

        if classification == "argument":
            return "audio_spike", round(min(db / 100.0, 0.96), 2)
        elif db >= AUDIO_HIGH_CONFIDENCE_DB and sustained >= AUDIO_SUSTAINED_SEC:
            return "audio_spike", round(min(db / 100.0, 0.92), 2)
        elif classification in ("very_loud", "loud") and db >= AUDIO_HIGH_DB:
            return "audio_spike", round(min(db / 100.0, 0.80), 2)
        return None, 0.0

    audio_df = audio_df.copy()
    audio_df["audio_label"], audio_df["audio_score"] = zip(*audio_df.apply(classify, axis=1))
    events = audio_df[audio_df["audio_label"].notna()].copy()
    print(f"  Audio events: {len(events)} ({events['audio_label'].value_counts().to_dict()})")
    return events


# ─── STEP 5: SIGNAL FUSION ─────────────────────────────────────────────────────

def fuse_signals(motion_events: pd.DataFrame,
                 audio_events: pd.DataFrame,
                 trips_df: pd.DataFrame) -> pd.DataFrame:
    """
    Combine motion and audio events into flagged moments.

    Fusion logic:
        - For each motion event, check for an audio event within ±COMBO_WINDOW_SEC
          in the same trip. If found, compute a weighted combined score.
        - For audio events with no nearby motion, create audio-only flags.

    Combined score = (motion_score × 0.55) + (audio_score × 0.45)
    Motion is weighted higher because it is physics-based and less ambiguous.

    Severity mapping:
        combined >= 0.75 → HIGH    + flag_type = conflict_moment
        combined >= 0.55 → MEDIUM
        else             → LOW
    """
    flagged: list[dict] = []

    # --- Motion-anchored events (with optional audio context) ---
    for trip_id, trip_motion in motion_events.groupby("trip_id"):
        trip_audio = audio_events[audio_events["trip_id"] == trip_id]
        trip_row = trips_df[trips_df["trip_id"] == trip_id]
        if trip_row.empty:
            continue

        driver_id  = trip_row.iloc[0]["driver_id"]
        trip_date  = trip_row.iloc[0]["date"]
        trip_start = trip_row.iloc[0]["start_time"]

        for _, m in trip_motion.iterrows():
            m_elapsed = m["elapsed_seconds"]
            m_score   = m["motion_score"]
            m_label   = m["motion_label"]

            # Look for overlapping audio
            nearby_audio = trip_audio[
                (trip_audio["elapsed_seconds"] >= m_elapsed - COMBO_WINDOW_SEC) &
                (trip_audio["elapsed_seconds"] <= m_elapsed + COMBO_WINDOW_SEC)
            ]

            if not nearby_audio.empty:
                best_audio  = nearby_audio.loc[nearby_audio["audio_score"].idxmax()]
                a_score     = best_audio["audio_score"]
                a_label     = best_audio["audio_label"]
                combined    = round(m_score * COMBO_WEIGHT_MOTION + a_score * COMBO_WEIGHT_AUDIO, 2)
                accel_mag   = round(m["horizontal_magnitude"], 1)
                db_val      = best_audio["audio_level_db"]

                if combined >= HIGH_SEVERITY_THRESHOLD:
                    severity   = "high"
                    flag_type  = "conflict_moment"
                    explanation = (
                        f"Combined signal: {m_label.replace('_',' ').title()} ({accel_mag} m/s²) "
                        f"+ sustained cabin audio ({db_val} dB). Potential passenger conflict."
                    )
                elif combined >= MEDIUM_SEVERITY_THRESHOLD:
                    severity   = "medium"
                    flag_type  = m_label
                    explanation = (
                        f"{m_label.replace('_',' ').title()} ({accel_mag} m/s²) "
                        f"with elevated audio ({db_val} dB). Possibly difficult traffic conditions."
                    )
                else:
                    severity   = "low"
                    flag_type  = m_label
                    explanation = (
                        f"{m_label.replace('_',' ').title()} ({accel_mag} m/s²) detected. "
                        f"Audio slightly elevated ({db_val} dB). Routine traffic event."
                    )

                context = f"Motion: {m_label} | Audio: {best_audio['audio_classification']}"
            else:
                # Motion only — no audio context
                a_score  = 0.18
                combined = round(m_score * 0.60, 2)
                severity = "medium" if m_score >= 0.65 else "low"
                flag_type = m_label
                accel_mag = round(m["horizontal_magnitude"], 1)
                explanation = (
                    f"{m_label.replace('_',' ').title()} detected ({accel_mag} m/s²). "
                    f"Motion-only signal, no elevated audio."
                )
                context = f"Motion: {m_label} | Audio: normal"

            flagged.append({
                "trip_id":          trip_id,
                "driver_id":        driver_id,
                "date":             trip_date,
                "trip_start":       trip_start,
                "elapsed_seconds":  int(m_elapsed),
                "flag_type":        flag_type,
                "severity":         severity,
                "motion_score":     m_score,
                "audio_score":      a_score,
                "combined_score":   combined,
                "explanation":      explanation,
                "context":          context,
            })

    # --- Audio-only events (no nearby motion found) ---
    for trip_id, trip_audio_g in audio_events.groupby("trip_id"):
        trip_row = trips_df[trips_df["trip_id"] == trip_id]
        if trip_row.empty:
            continue

        driver_id  = trip_row.iloc[0]["driver_id"]
        trip_date  = trip_row.iloc[0]["date"]
        trip_start = trip_row.iloc[0]["start_time"]
        trip_motion = motion_events[motion_events["trip_id"] == trip_id]

        for _, a in trip_audio_g.iterrows():
            a_elapsed = a["elapsed_seconds"]
            nearby_motion = trip_motion[
                (trip_motion["elapsed_seconds"] >= a_elapsed - COMBO_WINDOW_SEC) &
                (trip_motion["elapsed_seconds"] <= a_elapsed + COMBO_WINDOW_SEC)
            ]
            if not nearby_motion.empty:
                continue  # Already handled above in motion-anchored loop

            a_score  = a["audio_score"]
            combined = round(a_score * 0.55, 2)
            severity = "high" if a_score >= 0.85 else ("medium" if a_score >= 0.70 else "low")

            flagged.append({
                "trip_id":          trip_id,
                "driver_id":        driver_id,
                "date":             trip_date,
                "trip_start":       trip_start,
                "elapsed_seconds":  int(a_elapsed),
                "flag_type":        "audio_spike",
                "severity":         severity,
                "motion_score":     0.20,
                "audio_score":      a_score,
                "combined_score":   combined,
                "explanation":      (
                    f"Sustained elevated cabin audio ({a['audio_level_db']} dB). "
                    f"Audio-only signal — no concurrent motion event."
                ),
                "context":          f"Motion: normal | Audio: {a['audio_classification']}",
            })

    flagged_df = pd.DataFrame(flagged)

    # Add computed timestamp
    def compute_ts(row):
        try:
            base = datetime.strptime(f"{row['date']} {row['trip_start']}", "%Y-%m-%d %H:%M:%S")
            return (base + timedelta(seconds=int(row["elapsed_seconds"]))).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return f"{row['date']} {row['trip_start']}"

    flagged_df["timestamp"] = flagged_df.apply(compute_ts, axis=1)

    # Reorder columns to match reference format
    col_order = [
        "flag_id", "trip_id", "driver_id", "timestamp", "elapsed_seconds",
        "flag_type", "severity", "motion_score", "audio_score", "combined_score",
        "explanation", "context", "date", "trip_start",
    ]
    flagged_df.insert(0, "flag_id", [f"FLAG{str(i+1).zfill(3)}" for i in range(len(flagged_df))])
    flagged_df = flagged_df[[c for c in col_order if c in flagged_df.columns]]

    print(f"  Total flags: {len(flagged_df)}")
    print(f"  By type:     {flagged_df['flag_type'].value_counts().to_dict()}")
    print(f"  By severity: {flagged_df['severity'].value_counts().to_dict()}")
    return flagged_df


# ─── STEP 6: TRIP SUMMARIES ────────────────────────────────────────────────────

def build_trip_summaries(trips_df: pd.DataFrame,
                         flagged_df: pd.DataFrame,
                         motion_events: pd.DataFrame,
                         audio_events: pd.DataFrame) -> pd.DataFrame:
    """
    Generate a post-trip report card for every completed trip.

    Stress score:
        mean(combined_score of flags) + count_boost (0.05 per flag, max 0.20)
        Trips with no sensor data receive a low baseline score (ambient noise).

    Quality rating:
        poor      → stress > 0.6 or high severity flag exists
        fair      → stress > 0.35 or >= 2 flags
        good      → stress > 0.15 or >= 1 flag
        excellent → no flags, low stress
    """
    summaries = []
    sev_order = {"low": 1, "medium": 2, "high": 3}

    for _, trip in trips_df.iterrows():
        tid = trip["trip_id"]
        trip_flags  = flagged_df[flagged_df["trip_id"] == tid]
        trip_motion = motion_events[motion_events["trip_id"] == tid]
        trip_audio  = audio_events[audio_events["trip_id"] == tid]

        motion_count = len(trip_motion[trip_motion["motion_label"].notna()]) if "motion_label" in trip_motion.columns else 0
        audio_count  = len(trip_audio[trip_audio["audio_label"].notna()]) if "audio_label" in trip_audio.columns else 0
        flag_count   = len(trip_flags)

        # Severity rollup
        if flag_count == 0:
            max_sev = "none"
        else:
            max_sev_num = trip_flags["severity"].map(sev_order).max()
            max_sev = {v: k for k, v in sev_order.items()}[max_sev_num]

        # Stress score
        if flag_count > 0:
            base_stress  = trip_flags["combined_score"].mean()
            count_boost  = min(flag_count * 0.05, 0.20)
            stress_score = round(float(min(base_stress + count_boost, 0.99)), 2)  # type: ignore
        else:
            stress_score = round(np.random.uniform(0.03, 0.12), 2)

        # Quality rating
        if max_sev == "high" or stress_score > 0.60:
            quality = "poor"
        elif stress_score > 0.35 or flag_count >= 2:
            quality = "fair"
        elif stress_score > 0.15 or flag_count >= 1:
            quality = "good"
        else:
            quality = "excellent"

        # Earnings velocity (₹/hr for this trip)
        ev = round((trip["fare"] / trip["duration_min"]) * 60, 2)

        summaries.append({
            "trip_id":               tid,
            "driver_id":             trip["driver_id"],
            "date":                  trip["date"],
            "duration_min":          trip["duration_min"],
            "distance_km":           trip["distance_km"],
            "fare":                  trip["fare"],
            "pickup_location":       trip.get("pickup_location", ""),
            "dropoff_location":      trip.get("dropoff_location", ""),
            "earnings_velocity":     ev,
            "motion_events_count":   motion_count,
            "audio_events_count":    audio_count,
            "flagged_moments_count": flag_count,
            "max_severity":          max_sev,
            "stress_score":          stress_score,
            "trip_quality_rating":   quality,
        })

    return pd.DataFrame(summaries)


# ─── STEP 7: EARNINGS VELOCITY FORECAST ───────────────────────────────────────

def forecast_earnings_goals(goals_df: pd.DataFrame) -> pd.DataFrame:
    """
    For each driver's daily goal, compute:
        - current_velocity (₹/hr) = current_earnings / elapsed_hours
        - projected_earnings = current_earnings + velocity × remaining_hours
        - forecast_status: ahead / on_track / at_risk

    Assumes constant velocity (linear model). In production, a 30-min rolling
    window would give a more accurate short-term projection.
    """
    def forecast(row):
        earned    = row["current_earnings"]
        elapsed   = row["current_hours"]
        target    = row["target_earnings"]
        total_hrs = row["target_hours"]

        if elapsed <= 0:
            return "unknown", 0.0, float(earned)

        velocity  = earned / elapsed
        remaining = max(total_hrs - elapsed, 0)
        projected = earned + velocity * remaining
        gap       = projected - target

        if gap >= target * 0.10:
            status = "ahead"
        elif gap >= 0:
            status = "on_track"
        else:
            status = "at_risk"

        return status, round(velocity, 2), round(projected, 2)

    goals_df = goals_df.copy()
    goals_df["forecast_status"], goals_df["computed_velocity"], goals_df["projected_earnings"] = \
        zip(*goals_df.apply(forecast, axis=1))

    print(f"  Goal forecasts: {goals_df['forecast_status'].value_counts().to_dict()}")
    return goals_df


# ─── STEP 8: SAVE OUTPUTS ─────────────────────────────────────────────────────

def save_outputs(flagged_df: pd.DataFrame,
                 trip_summaries_df: pd.DataFrame,
                 goals_df: pd.DataFrame,
                 output_dir: str):
    """Write all output files to the output directory."""
    os.makedirs(output_dir, exist_ok=True)

    flagged_df.to_csv(f"{output_dir}/flagged_moments.csv", index=False)
    trip_summaries_df.to_csv(f"{output_dir}/trip_summaries.csv", index=False)
    goals_df.to_csv(f"{output_dir}/driver_goals_enriched.csv", index=False)

    print(f"\n  Outputs written to {output_dir}/")
    print(f"    flagged_moments.csv    — {len(flagged_df)} rows")
    print(f"    trip_summaries.csv     — {len(trip_summaries_df)} rows")
    print(f"    driver_goals_enriched  — {len(goals_df)} rows")


# ─── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("DRIVER PULSE — Signal Processing Pipeline")
    print("=" * 60)

    print("\n[1/7] Ingesting raw data...")
    data = ingest(DATA_DIR)

    print("\n[2/7] Extracting motion features...")
    acc_featured = extract_motion_features(data["acc"])

    print("\n[3/7] Detecting motion events...")
    motion_events = detect_motion_events(acc_featured)

    print("\n[4/7] Detecting audio events...")
    audio_events = detect_audio_events(data["audio"])

    print("\n[5/7] Fusing signals into flagged moments...")
    flagged_df = fuse_signals(motion_events, audio_events, data["trips"])

    print("\n[6/7] Building trip summaries...")
    trip_summaries_df = build_trip_summaries(
        data["trips"], flagged_df, motion_events, audio_events
    )

    print("\n[7/7] Forecasting earnings goals...")
    goals_enriched = forecast_earnings_goals(data["goals"])

    print("\n[8/8] Saving outputs...")
    save_outputs(flagged_df, trip_summaries_df, goals_enriched, OUTPUT_DIR)

    print("\n✅ Pipeline complete.")
    print("=" * 60)


if __name__ == "__main__":
    main()

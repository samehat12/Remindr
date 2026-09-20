import numpy as np

import pandas as pd
import numpy as np

class VoloridgeDataEngine:
    def __init__(self, data_folder="."):
        # 1. Load patient metadata
        self.patients_df = pd.read_csv(f"{data_folder}/patients.csv")
        
        # 2. Load historical routine logs
        self.routines_df = pd.read_csv(f"{data_folder}/routine_events.csv")
        
        # 3. Load daily cognitive metrics
        self.signals_df = pd.read_csv(f"{data_folder}/daily_cognitive_signals.csv")

    def get_z_scores_list(self, column_name: str = "reply_latency_minutes") -> list[float]:
        """Calculate z-scores for every non-null value in a numeric column, sorted by magnitude."""
        if column_name not in self.routines_df.columns:
            raise ValueError(f"Column '{column_name}' does not exist in routine_events.csv.")

        numeric_values = pd.to_numeric(self.routines_df[column_name], errors="coerce").dropna()
        if numeric_values.empty:
            return []

        mean = float(numeric_values.mean())
        std = float(numeric_values.std(ddof=0))

        if std == 0:
            return [0.0] * len(numeric_values)

        z_scores = [
            calculate_z_score(float(value), mean, std)
            for value in numeric_values
        ]

        return sorted(z_scores, key=abs, reverse=True)

    def det_risk_level_with_z_score(
        self,
        z_score: float,
        column_name: str = "reply_latency_minutes",
    ) -> str:
        """Classify a z-score by splitting historical z-score magnitudes into tertiles."""
        z_scores = self.get_z_scores_list(column_name)
        if not z_scores:
            risk_level = "NOMINAL"
            return risk_level

        magnitude_groups = np.array_split(
            np.array([abs(score) for score in z_scores], dtype=float),
            3,
        )

        critical_group = magnitude_groups[0]
        elevated_group = magnitude_groups[1]
        absolute_z_score = abs(z_score)

        critical_threshold = float(critical_group.min()) if len(critical_group) > 0 else float("inf")
        elevated_threshold = float(elevated_group.min()) if len(elevated_group) > 0 else float("inf")

        if absolute_z_score >= critical_threshold:
            risk_level = "CRITICAL"
        elif absolute_z_score >= elevated_threshold:
            risk_level = "ELEVATED"
        else:
            risk_level = "NOMINAL"

        return risk_level

    def get_patient_baseline(self, patient_id: str, event_type: str | None = None):
        """Extract a patient's reply-latency baseline from routine_events.csv."""
        patient_events = self.routines_df[self.routines_df["patient_id"] == patient_id]

        if event_type is not None and "task_type" in patient_events.columns:
            matching_events = patient_events[patient_events["task_type"] == event_type]
            if not matching_events.empty:
                patient_events = matching_events

        if patient_events.empty:
            return {"mean": 15.0, "std": 5.0}

        latency_values = pd.to_numeric(patient_events["reply_latency_minutes"], errors="coerce").dropna()
        if latency_values.empty:
            return {"mean": 15.0, "std": 5.0}

        mean_delay = float(latency_values.mean())
        std_delay = float(latency_values.std(ddof=0))

        return {
            "mean": round(mean_delay, 2),
            "std": round(std_delay, 2) if std_delay > 0 else 1.0,
        }

    def evaluate_live_event(self, patient_id: str, current_delay: float, confusion_score: float = 0.0):
        """Calculates Z-score using the synthetic baseline and assesses risk level."""
        baseline = self.get_patient_baseline(patient_id)
        
        # Z-score formula: (x - mean) / std
        z_score = (current_delay - baseline["mean"]) / baseline["std"]
        z_score = round(z_score, 2)
        status = self.det_risk_level_with_z_score(z_score)
        alert = status == "CRITICAL"
             
        return {
            "patient_id": patient_id,
            "current_delay": current_delay,
            "baseline_mean": baseline["mean"],
            "z_score": z_score,
            "confusion_score": confusion_score,
            "status": status,
            "trigger_caregiver_alert": alert
        }


    def calculate_z_score(current_value: float, mean: float, std: float) -> float:
        """Computes the statistical Z-score for an observed value."""
        if std == 0:
            return 0.0
        z_score = (current_value - mean) / std
        return round(z_score, 2)

#longer patient_risk_eval code with more features
# def evaluate_patient_risk(z_score: float, confusion_score: float = 0.0, task_missed: bool = False) -> dict:
#     """Evaluates combined signals and returns risk classification."""
#     if task_missed or z_score > 2.5 or confusion_score > 0.8:
#         risk_level = "CRITICAL"
#         action = "TRIGGER_CAREGIVER_ALERT"
#     elif z_score > 1.5 or confusion_score > 0.5:
#         risk_level = "ELEVATED"
#         action = "SEND_GENTLE_REMINDER"
#     else:
#         risk_level = "NOMINAL"
#         action = "LOG_NORMAL_ACTIVITY"
        
#     return {
#         "z_score": z_score,
#         "confusion_score": confusion_score,
#         "risk_level": risk_level,
#         "recommended_action": action
#     }

    def apply_kalman_filter(data: list[float], Q: float = 1e-5, R: float = 0.1) -> list[float]:
        """1D Kalman filter to smooth noisy sensor/latency streams."""
        x_hat = data[0] if data else 0.0  # Initial state estimate
        P = 1.0  # Initial estimate uncertainty
        
        smoothed = []
        for z in data:
            # Prediction update
            P = P + Q
            # Measurement update (Kalman Gain)
            K = P / (P + R)
            x_hat = x_hat + K * (z - x_hat)
            P = (1 - K) * P
            smoothed.append(round(x_hat, 2))
            
        return smoothed

if __name__ == "__main__":
    # Automatically gets z-scores for the default column "reply_latency_minutes".
    engine = VoloridgeDataEngine(data_folder="synthetic_data")
    print(engine.get_z_scores_list())

# test_anomaly.py
from data_processor import VoloridgeDataEngine  # Import your class from data_processor.py

# 1. Initialize your engine pointing to your teammate's synthetic data subfolder
engine = VoloridgeDataEngine(data_folder="synthetic_data")

# 2. Simulate patient SYN-002 taking 90 minutes to confirm medication (normal mean is ~15 mins)
print("--- RUNNING SIMULATED ANOMALY TEST FOR PATIENT SYN-002 ---")
result = engine.evaluate_live_event(
    patient_id="SYN-002", 
    current_delay=90.0,      # 90 minutes late!
    confusion_score=0.85     # High confusion detected in text
)

# 3. Print the output to verify your Z-score and alert status
print(f"Patient ID:     {result['patient_id']}")
print(f"Current Delay:  {result['current_delay']} mins (Baseline Avg: {result['baseline_mean']} mins)")
print(f"Z-Score:        {result['z_score']}")
print(f"Risk Status:    {result['status']}")
print(f"Trigger Alert?  {result['trigger_caregiver_alert']}")
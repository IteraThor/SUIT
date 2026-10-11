"""Autonomous Multi-Agent Calibration Loop Orchestrator."""

from __future__ import annotations
import json
import os
import subprocess
import time
from dataclasses import dataclass, asdict
from typing import List, Dict, Any, Optional

from core.vision_critic import VisionCritic
from core.recognition_arbiter import RecognitionArbiter


@dataclass
class LedgerEntry:
    iteration: int
    hypothesis: str
    parameters: Dict[str, Any]
    approved: bool
    composite_gain: float
    notes: str


class CalibrationLedger:
    def __init__(self, ledger_path: str = "tools/autocalib_ledger.json"):
        self.ledger_path = ledger_path

    def record(self, entry: LedgerEntry):
        entries = self.load_all()
        entries.append(entry)
        with open(self.ledger_path, "w") as f:
            json.dump([asdict(e) for e in entries], f, indent=2)

    def load_all(self) -> List[LedgerEntry]:
        if not os.path.exists(self.ledger_path):
            return []
        try:
            with open(self.ledger_path) as f:
                data = json.load(f)
                return [LedgerEntry(**d) for d in data]
        except Exception:
            return []


class AutoCalibOrchestrator:
    def __init__(
        self,
        remote_host: str = "192.168.2.103",
        user: str = "autodarts",
        password: str = "autodarts",
    ):
        self.remote_host = remote_host
        self.user = user
        self.password = password
        self.critic = VisionCritic()
        self.arbiter = RecognitionArbiter()
        self.ledger = CalibrationLedger()

    def run_remote_command(self, cmd: str) -> str:
        full_cmd = f"sshpass -p {self.password} ssh -o StrictHostKeyChecking=no {self.user}@{self.remote_host} \"{cmd}\""
        res = subprocess.run(full_cmd, shell=True, capture_output=True, text=True)
        return res.stdout.strip()

    def execute_remote_sweep(self) -> Optional[str]:
        cmd = "python3 -c \"import glob, os; print(max(glob.glob('/home/autodarts/.local/share/suit/calibration_sessions/session_*'), key=os.path.getctime, default=''))\""
        out = self.run_remote_command(cmd)
        return out if out else None

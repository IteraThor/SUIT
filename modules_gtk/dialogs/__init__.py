"""
Dialog components for SUIT GTK4 views.
"""
from modules_gtk.dialogs.debloat_dialog import DebloatReviewDialog
from modules_gtk.dialogs.tailscale_dialog import TailscaleAuthDialog
from modules_gtk.dialogs.community_dialog import CommunityDialog
from modules_gtk.dialogs.board_setup_dialog import BoardSetupDialog

try:
    from modules_gtk.private.darts_scorer_dialog import DartsScorerInstallDialog
except ImportError:
    DartsScorerInstallDialog = None

__all__ = [
    "DebloatReviewDialog",
    "TailscaleAuthDialog",
    "CommunityDialog",
    "BoardSetupDialog",
]
if DartsScorerInstallDialog is not None:
    __all__.append("DartsScorerInstallDialog")


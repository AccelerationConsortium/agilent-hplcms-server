"""Copy only preview assets, not repository files or archived versions, to dist."""
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODEL = ROOT.parent
DIST = ROOT / 'dist'
DIST.mkdir(exist_ok=True)
data = json.loads((MODEL / 'dimensions.json').read_text())
validation = json.loads((MODEL / 'validation.json').read_text())
for filename in ('body_draft.stl', 'body_draft.step', 'body_preview.png'):
    shutil.copy2(MODEL / filename, DIST / filename)
metadata = {
    'revision': data['revision'],
    'measured': data['measured'],
    'validation': validation,
    'pending_rear_slots': data.get('pending_features', {}).get('rear_slots', []),
    'note': 'v30: Notches corrected to extend FORWARD from rear wall of side opening Y88.50. Rear base-cutout notch LEFT3.5 x3.5 mm, RIGHT1.5 x2 mm, forward x inward, through base. Rear base cutouts8 mm inward, taper13 to2.5 mm; inner flat centered and sides mirrored provisionally. Rear side openings13 mm long, top9.60, flat top9, front edge5, rear setback8.90. Opposite side mirrored provisionally; base continuation now modeled. Front wall follows both corner chamfers with2 mm perpendicular thickness. Top-view corner chamfers: rear-right1 mm along back x2 mm along side; both front corners2 mm along front x4 mm along side. Both front holes6 x3.3 mm, front setback2 mm, side setback6.6 mm. Front slopes await measurements. Right vertical step aligned with inner rear platform front at88.40 mm; preceding flat8.40 mm and vertical rise10 mm. Right rear return front edge4.30 mm from rear outer face; slope joins at34.30 mm height. Return uses existing2 mm side thickness; fine bevels remain unmeasured. Left slope45 degrees from65 mm. Wing shifted2.30 mm forward, top flush with frame at48.20 mm, bottom22.85 mm; a provisional2 mm return joins rear panel. Two end slots added: 10 x 3 mm, rear setback 3 mm, side setback 4.25 mm. Opposite end mirrored provisionally; slots pass through the ledge. A/C deferred. B is now a diameter 10 mm, depth 3 mm blind magnet pocket, centered 6 mm from rear face. Local support retains a 1.5 mm floor; nominal dimensions without fit clearance. Existing left/right 60 degree tab bevels retained.',
}
target = DIST / 'metadata.json'
temp = target.with_suffix('.tmp')
temp.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + '\n')
temp.replace(target)
print(f'Preview synchronized: v{data["revision"]}')

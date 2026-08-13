"""
Graphica — Vérificateur couleur / noir & blanc pour impression
Analyse chaque page d'un PDF pour déterminer si elle est en couleur
ou en noir & blanc, en se basant sur:
  1. L'espace colorimétrique de l'image (DeviceGray = N&B garanti)
  2. Une analyse pixel par pixel (avec tolérance JPEG) pour les images RGB/CMYK,
     afin d'attraper les petits éléments colorés (logos, surlignage, etc.)

Ce fichier est le backend destiné à être déployé sur Render.
"""

import io
import os
import tempfile
import time
import traceback

import fitz  # PyMuPDF
import numpy as np
from flask import Flask, jsonify, request
from flask_cors import CORS
from PIL import Image

app = Flask(__name__)

# ---------------------------------------------------------------------------
# CORS: restreint aux domaines du site Wix (ajuster via variable d'env)
# ---------------------------------------------------------------------------
ALLOWED_ORIGINS = os.environ.get(
    "ALLOWED_ORIGINS",
    "https://www.graphica.qc.ca,https://graphica.qc.ca"
).split(",")
CORS(app, origins=ALLOWED_ORIGINS)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "300"))
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

# Seuils de détection (identiques à l'analyse validée manuellement)
DIFF_THRESHOLD = 12       # écart max-min de canal RGB toléré (bruit JPEG)
MIN_COLORED_PIXELS = 40   # nombre min de pixels "colorés" avant de conclure COULEUR
DRAFT_SIZE = (850, 1100)  # taille de décodage rapide (draft JPEG)


def analyze_page(doc, page_index):
    """Analyse une page et retourne son statut couleur / N&B."""
    page = doc[page_index]
    imgs = page.get_images(full=True)

    if not imgs:
        # Page sans image plein format : on se rabat sur les objets vectoriels
        drawings = page.get_drawings()
        for d in drawings:
            for key in ("color", "fill"):
                c = d.get(key)
                if c and len(c) >= 3:
                    r, g, b = c[0], c[1], c[2]
                    if max(r, g, b) - min(r, g, b) > 0.02:
                        return {
                            "page": page_index + 1,
                            "colorspace": "vector",
                            "status": "COULEUR",
                            "colored_pct": None,
                        }
        return {
            "page": page_index + 1,
            "colorspace": "aucune image",
            "status": "N&B",
            "colored_pct": 0.0,
        }

    xref = imgs[0][0]
    base = doc.extract_image(xref)
    cs = base.get("cs-name")
    img_bytes = base["image"]

    if cs == "DeviceGray":
        return {
            "page": page_index + 1,
            "colorspace": cs,
            "status": "N&B",
            "colored_pct": 0.0,
        }

    try:
        im = Image.open(io.BytesIO(img_bytes))
        im.draft("RGB", DRAFT_SIZE)
        im = im.convert("RGB")
        arr = np.array(im)
        diff = arr.max(axis=2).astype(int) - arr.min(axis=2).astype(int)
        colored_mask = diff > DIFF_THRESHOLD
        colored_px = int(colored_mask.sum())
        total_px = arr.shape[0] * arr.shape[1]
        pct = colored_px / total_px * 100

        status = "COULEUR" if colored_px >= MIN_COLORED_PIXELS else "N&B"

        return {
            "page": page_index + 1,
            "colorspace": cs,
            "status": status,
            "colored_pct": round(pct, 4),
        }
    except Exception as e:
        return {
            "page": page_index + 1,
            "colorspace": cs,
            "status": "ERREUR",
            "colored_pct": None,
            "error": str(e),
        }


def build_ranges(results):
    """Regroupe les pages consécutives de même statut en plages."""
    ranges = []
    cur_status = None
    start = None
    prev = None
    for r in results:
        if r["status"] != cur_status:
            if cur_status is not None:
                ranges.append({"start": start, "end": prev, "status": cur_status})
            cur_status = r["status"]
            start = r["page"]
        prev = r["page"]
    if cur_status is not None:
        ranges.append({"start": start, "end": prev, "status": cur_status})
    return ranges


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/api/analyze", methods=["POST"])
def analyze():
    if "file" not in request.files:
        return jsonify({"error": "Aucun fichier reçu."}), 400

    f = request.files["file"]
    if f.filename == "":
        return jsonify({"error": "Aucun fichier sélectionné."}), 400

    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Le fichier doit être un PDF."}), 400

    t_start = time.time()

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        f.save(tmp.name)
        tmp_path = tmp.name

    try:
        doc = fitz.open(tmp_path)
        n_pages = len(doc)

        if n_pages == 0:
            return jsonify({"error": "Le PDF ne contient aucune page."}), 400

        results = [analyze_page(doc, i) for i in range(n_pages)]
        doc.close()

        nb = sum(1 for r in results if r["status"] == "N&B")
        coul = sum(1 for r in results if r["status"] == "COULEUR")
        err = sum(1 for r in results if r["status"] == "ERREUR")

        ranges = build_ranges(results)
        elapsed = round(time.time() - t_start, 1)

        return jsonify({
            "filename": f.filename,
            "total_pages": n_pages,
            "pages_nb": nb,
            "pages_couleur": coul,
            "pages_erreur": err,
            "processing_seconds": elapsed,
            "ranges": ranges,
            "pages": results,
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"Erreur lors de l'analyse du PDF : {str(e)}"}), 500

    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

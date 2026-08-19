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
import re
import smtplib
import tempfile
import time
import traceback
from email.mime.text import MIMEText

import fitz  # PyMuPDF
import numpy as np
import requests
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

# ---------------------------------------------------------------------------
# Envoi de courriel (relais SMTP Microsoft 365) — pour le rapport par courriel
# ---------------------------------------------------------------------------
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.office365.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")          # ex: noreply@graphica.qc.ca
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")  # mot de passe applicatif M365
FROM_EMAIL = os.environ.get("FROM_EMAIL", SMTP_USER)

# ---------------------------------------------------------------------------
# Journalisation des analyses (Google Sheet via Apps Script webhook)
# ---------------------------------------------------------------------------
SHEET_WEBHOOK_URL = os.environ.get("SHEET_WEBHOOK_URL")  # URL /exec du script

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def log_to_sheet(action, data):
    """Best-effort logging to Google Sheet. Never blocks or fails the request."""
    if not SHEET_WEBHOOK_URL:
        return
    try:
        payload = {"action": action, **data}
        requests.post(SHEET_WEBHOOK_URL, json=payload, timeout=5)
    except Exception:
        # La journalisation ne doit jamais faire planter une vraie requête utilisateur
        traceback.print_exc()


def send_report_email(to_email, filename, total_pages, pages_nb, pages_couleur, ranges):
    if not SMTP_USER or not SMTP_PASSWORD:
        raise RuntimeError("Configuration SMTP manquante sur le serveur.")

    pct_couleur = round(pages_couleur / total_pages * 100) if total_pages else 0
    pct_nb = 100 - pct_couleur

    range_lines = []
    for r in ranges:
        label = f"Page {r['start']}" if r["start"] == r["end"] else f"Pages {r['start']}–{r['end']}"
        count = r["end"] - r["start"] + 1
        status = "Couleur" if r["status"] == "COULEUR" else "Noir & blanc"
        range_lines.append(f"  {label} ({count} pages) — {status}")

    body = (
        f"Voici le rapport d'analyse pour votre document : {filename}\n\n"
        f"Total des pages : {total_pages}\n"
        f"Couleur : {pages_couleur} pages ({pct_couleur}%)\n"
        f"Noir & blanc : {pages_nb} pages ({pct_nb}%)\n\n"
        f"Répartition par plage de pages :\n" + "\n".join(range_lines) + "\n\n"
        f"---\n"
        f"Graphica impression inc. — Cet outil sert d'estimation. "
        f"La facturation finale est établie par notre équipe de production.\n"
        f"graphica.qc.ca"
    )

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"Votre rapport d'analyse couleur — {filename}"
    msg["From"] = FROM_EMAIL
    msg["To"] = to_email

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(FROM_EMAIL, [to_email], msg.as_string())

# Seuils de détection (identiques à l'analyse validée manuellement)
DIFF_THRESHOLD = 12       # écart max-min de canal RGB toléré (bruit JPEG)
MIN_COLORED_PIXELS = 40   # nombre min de pixels "colorés" avant de conclure COULEUR
DRAFT_SIZE = (850, 1100)  # taille de décodage rapide (draft JPEG)
BLANK_NONWHITE_PCT = 0.05  # % de pixels non-blancs en dessous duquel une page est "blanche"
WHITE_THRESHOLD = 250      # valeur de gris (0-255) au-dessus de laquelle un pixel est "blanc"
BLANK_CHECK_DPI = 20       # résolution très basse, juste pour détecter une page vide (rapide)


def format_dimension(value_in):
    """Formate une dimension en pouces à la française (virgule, pas de .0 inutile)."""
    v = round(value_in, 1)
    if v == int(v):
        return str(int(v))
    return f"{v:.1f}".replace(".", ",")


def get_format_label(width_pt, height_pt):
    """
    Convertit les dimensions de page (points PDF) en étiquette lisible, ex. '8,5 × 11 po'.
    Normalise l'ordre (plus petite dimension en premier) pour qu'une même feuille
    physique en orientation portrait ou paysage produise toujours la même étiquette.
    """
    width_in = width_pt / 72
    height_in = height_pt / 72
    short, long = sorted([width_in, height_in])
    return f"{format_dimension(short)} × {format_dimension(long)} po"


def check_is_blank(page):
    """
    Détecte une page vide en rendant la page entière à très basse résolution
    (rapide — ~0,02s/page) plutôt qu'en décodant l'image intégrale, ce qui
    capture aussi tout élément vectoriel superposé, pas seulement l'image.
    """
    try:
        pix = page.get_pixmap(dpi=BLANK_CHECK_DPI)
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        gray = arr[:, :, :3].mean(axis=2) if pix.n >= 3 else arr[:, :, 0]
        total_px = gray.shape[0] * gray.shape[1]
        non_white_px = int((gray < WHITE_THRESHOLD).sum())
        return (non_white_px / total_px * 100) < BLANK_NONWHITE_PCT
    except Exception:
        return False


def check_has_colored_text(page, tolerance=5):
    """
    Vérifie si la page contient du texte dont la couleur n'est pas neutre
    (ni noir, ni gris). Ceci est distinct des images et des formes vectorielles —
    du texte coloré (ex. un lien bleu, une signature électronique, un titre en
    couleur) est un objet PDF à part et n'était auparavant jamais vérifié
    lorsque la page contenait déjà une image.
    """
    try:
        d = page.get_text("dict")
    except Exception:
        return False

    for block in d.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                color_int = span.get("color", 0)
                r = (color_int >> 16) & 255
                g = (color_int >> 8) & 255
                b = color_int & 255
                if max(r, g, b) - min(r, g, b) > tolerance:
                    return True
    return False


def check_has_colored_vectors(page, tolerance=0.02):
    """
    Vérifie si la page contient des formes vectorielles (rectangles, traits,
    remplissages) dont la couleur n'est pas neutre. IMPORTANT : ceci doit
    être vérifié sur TOUTE page, pas seulement celles sans image — un plan
    d'architecte, un tableau ou un diagramme peut avoir une petite image
    (logo, filigrane) tout en contenant des illustrations vectorielles en
    couleur ailleurs sur la page. Vérifier les vecteurs seulement en
    l'absence d'image manquait exactement ce cas.
    """
    try:
        drawings = page.get_drawings()
    except Exception:
        return False

    for d in drawings:
        for key in ("color", "fill"):
            c = d.get(key)
            if c and len(c) >= 3:
                r, g, b = c[0], c[1], c[2]
                if max(r, g, b) - min(r, g, b) > tolerance:
                    return True
    return False


def analyze_page(doc, page_index):
    """Analyse une page et retourne son statut couleur / N&B, son format et si elle est blanche."""
    page = doc[page_index]
    rect = page.rect
    fmt_label = get_format_label(rect.width, rect.height)
    is_blank = check_is_blank(page)
    has_colored_text = check_has_colored_text(page)
    has_colored_vector = check_has_colored_vectors(page)
    imgs = page.get_images(full=True)

    if not imgs:
        if has_colored_vector or has_colored_text:
            return {
                "page": page_index + 1,
                "colorspace": "vector/texte",
                "status": "COULEUR",
                "colored_pct": None,
                "format": fmt_label,
                "is_blank": False,
            }
        return {
            "page": page_index + 1,
            "colorspace": "aucune image",
            "status": "N&B",
            "colored_pct": 0.0,
            "format": fmt_label,
            "is_blank": is_blank,
        }

    xref = imgs[0][0]
    base = doc.extract_image(xref)
    cs = base.get("cs-name")
    img_bytes = base["image"]

    if cs == "DeviceGray":
        has_other_color = has_colored_text or has_colored_vector
        status = "COULEUR" if has_other_color else "N&B"
        return {
            "page": page_index + 1,
            "colorspace": cs if not has_other_color else "DeviceGray + vecteur/texte couleur",
            "status": status,
            "colored_pct": 0.0,
            "format": fmt_label,
            "is_blank": is_blank and not has_other_color,
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

        has_other_color = has_colored_text or has_colored_vector
        status = "COULEUR" if (colored_px >= MIN_COLORED_PIXELS or has_other_color) else "N&B"

        return {
            "page": page_index + 1,
            "colorspace": cs,
            "status": status,
            "colored_pct": round(pct, 4),
            "format": fmt_label,
            "is_blank": is_blank and not has_other_color,
        }
    except Exception as e:
        return {
            "page": page_index + 1,
            "colorspace": cs,
            "status": "ERREUR",
            "colored_pct": None,
            "format": fmt_label,
            "is_blank": False,
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


def build_format_breakdown(results):
    """
    Regroupe les pages par format, avec le compte Couleur / N&B / total / blanches
    pour chaque format — sert de base au tableau croisé affiché dans l'outil.
    Les pages blanches restent comptées dans N&B (pas une catégorie de facturation
    séparée), mais leur nombre est aussi suivi par format pour la note informative.
    """
    formats = {}  # format_label -> {couleur, nb, blank}
    order = []    # préserve l'ordre de première apparition

    for r in results:
        fmt = r.get("format", "?")
        if fmt not in formats:
            formats[fmt] = {"couleur": 0, "nb": 0, "blank": 0}
            order.append(fmt)

        if r["status"] == "COULEUR":
            formats[fmt]["couleur"] += 1
        elif r["status"] == "N&B":
            formats[fmt]["nb"] += 1

        if r.get("is_blank"):
            formats[fmt]["blank"] += 1

    breakdown = []
    for fmt in order:
        c = formats[fmt]
        breakdown.append({
            "format": fmt,
            "couleur": c["couleur"],
            "nb": c["nb"],
            "total": c["couleur"] + c["nb"],
            "blank": c["blank"],
        })

    total_blank = sum(c["blank"] for c in formats.values())
    blank_by_format = [
        {"format": fmt, "count": formats[fmt]["blank"]}
        for fmt in order if formats[fmt]["blank"] > 0
    ]

    return breakdown, total_blank, blank_by_format


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
        format_breakdown, total_blank, blank_by_format = build_format_breakdown(results)
        elapsed = round(time.time() - t_start, 1)

        log_to_sheet("analyse", {
            "filename": f.filename,
            "total_pages": n_pages,
            "pages_nb": nb,
            "pages_couleur": coul,
            "processing_seconds": elapsed,
        })

        return jsonify({
            "filename": f.filename,
            "total_pages": n_pages,
            "pages_nb": nb,
            "pages_couleur": coul,
            "pages_erreur": err,
            "processing_seconds": elapsed,
            "ranges": ranges,
            "format_breakdown": format_breakdown,
            "total_blank": total_blank,
            "blank_by_format": blank_by_format,
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


@app.route("/api/send-report", methods=["POST"])
def send_report():
    data = request.get_json(silent=True) or {}

    email = (data.get("email") or "").strip()
    if not email or not EMAIL_RE.match(email):
        return jsonify({"error": "Adresse courriel invalide."}), 400

    filename = data.get("filename", "document.pdf")
    total_pages = data.get("total_pages", 0)
    pages_nb = data.get("pages_nb", 0)
    pages_couleur = data.get("pages_couleur", 0)
    ranges = data.get("ranges", [])

    try:
        send_report_email(email, filename, total_pages, pages_nb, pages_couleur, ranges)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"Impossible d'envoyer le courriel : {str(e)}"}), 500

    log_to_sheet("envoi_courriel", {
        "filename": filename,
        "total_pages": total_pages,
        "pages_nb": pages_nb,
        "pages_couleur": pages_couleur,
        "email": email,
    })

    return jsonify({"success": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

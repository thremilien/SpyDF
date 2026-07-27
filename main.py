"""Local PDF redaction webapp.

    uv run main.py

Ouvre http://127.0.0.1:8765 dans le navigateur. Rien n'est envoye ailleurs,
tout tourne en local et en memoire.

Principe: on dessine des rectangles a la souris, et a l'export PyMuPDF
supprime reellement les objets texte sous chaque zone (apply_redactions)
au lieu de dessiner un carre par-dessus. Le reste du PDF garde sa couche
texte, il reste indexe et selectionnable.
"""

from src.server import main

if __name__ == "__main__":
    main()

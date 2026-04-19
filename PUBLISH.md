# Publication du CLI OpsHero sur PyPI

## Prérequis

1. Compte PyPI: https://pypi.org/account/register/
2. Token API PyPI: https://pypi.org/manage/account/token/

## Installation des outils de build

```bash
pip install build twine
```

## Étapes de publication

### 1. Nettoyer les anciens builds

```bash
cd cli
rm -rf dist/ build/ *.egg-info
```

### 2. Builder le package

```bash
python -m build
```

Cela crée deux fichiers dans `dist/`:
- `opshero-0.1.0-py3-none-any.whl` (wheel)
- `opshero-0.1.0.tar.gz` (source)

### 3. Vérifier le package

```bash
twine check dist/*
```

### 4. Publier sur PyPI

```bash
twine upload dist/*
```

Quand demandé:
- Username: `__token__`
- Password: ton token API PyPI (commence par `pypi-...`)

### 5. Vérifier l'installation

```bash
pip install opshero
opshero --version
```

## Mise à jour d'une nouvelle version

1. Modifier la version dans `pyproject.toml`
2. Nettoyer et rebuilder
3. Republier avec twine

## Test sur TestPyPI (optionnel)

Avant de publier sur PyPI, tu peux tester sur TestPyPI:

```bash
# Publier sur TestPyPI
twine upload --repository testpypi dist/*

# Installer depuis TestPyPI
pip install --index-url https://test.pypi.org/simple/ opshero
```

## Notes

- Le nom `opshero` doit être unique sur PyPI
- Si le nom est pris, change-le dans `pyproject.toml`
- La version doit être incrémentée à chaque publication
- Une fois publié, tu ne peux pas supprimer ou modifier une version

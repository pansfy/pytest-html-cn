@ECHO OFF
python -m pip install --upgrade pip
pip install . --no-cache-dir --progress-bar=off --use-feature=in-tree-build

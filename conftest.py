# Empty on purpose: its presence makes pytest add the repo root to
# sys.path in "prepend" import mode, so tests can `import news_pipeline`,
# `import monitor`, etc. without a src layout or installed package.

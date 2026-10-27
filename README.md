# gh-buildstatus

`gh-buildstatus` は、Yocto buildstats のタスク記録を棒グラフレース用 JSON に変換する
GitHub CLI 拡張です。

## 使い方

```sh
gh buildstatus raw2json --input <buildstats-dir> --output <json-file>
```

入力ディレクトリには `build_stats` と `raw/` が必要です。`raw/` ディレクトリ自体を
入力に指定することもできます。出力先の親ディレクトリが存在しない場合は作成します。

例:

```sh
gh buildstatus raw2json \
  --input tmp/log/host/buildstats/20261020142538 \
  --output docs/bar_chart_race.json
```

実行には Python 3 が必要です。変換処理には `commands/bar-chart-race.py` を使用します。

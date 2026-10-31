# 引き継ぎメモ（新しいチャットで最初に読む）

## 今の状態（2026/10/31）
- VPS（さくら、/opt/keiba、docker compose）で **本番（live）稼働中**。持ち主の指示で ALLOW_LIVE=1。
- 即PAT・SPAT4とも本物の画面で購入と照会の払戻まで確認済み（計700円のテスト購入）。
- 最新コミットは GitHub の main（680f740）。サーバーも同じ版。
- 買い方のルールは README.md の「買い方のルール」が正。

## VPSでの作業のしかた
- VPSはClaudeのリモート環境 `env_012UA9pStLksYwQjTeAzQvYM`（ブリッジ）で動く。作業が要るときだけ短いセッションを作り、終わったらアーカイブする（長い会話は使用量が多い）。
- 更新：`cd /opt/keiba && git pull && COMPOSE_BAKE=false docker compose up -d --build`
- 接続テスト：`docker compose exec -T app python -m tools.calibrate ipat|spat4 --venue 場 --race R --horse 1`
- サーバーには GitHub のログイン情報が無い。サーバーで作ったコミットは format-patch で取り出して、こちらで git am して push する。

## 守ること
- `.env` の中身（ID・暗証番号など）は表示しない。チャットにも書かせない。
- 報告は日本語で、専門用語を避ける。
- tmux の remote-control は Ctrl+C で止めない。

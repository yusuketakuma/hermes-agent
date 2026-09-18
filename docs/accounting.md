# Discord経由の請求管理

請求管理の正本は Finance Core です。`accounting` という旧ローカルツールは
登録・公開していません。Discordの自然言語入力は、Finance Core MCPの
`finance.*` ツールへ変換されます。

## 有効化

AccountingプラグインはFinance Core用のSkillだけを登録します。

```text
hermes plugins enable accounting
```

Finance Core MCPが起動していること、Discordの経理チャンネルに
`skill-fin-discord-finance-workflow` と `skill-fin-invoice-generation` が
紐付いていることを確認してください。設定変更後はGatewayを再起動します。

## 初回の承認者登録

実際のDiscordユーザーIDを設定ファイルへ手入力したり、表示名から推測したり
しません。認証済みDiscordセッションから、運用責任者が明示的に次を依頼します。

```text
このセッションの私をFinance Coreの管理者として初期登録して
```

Skillは `finance.approval.bootstrap_self` を呼び出します。初回は現在の
認証済みセッション自身だけが管理者になり、その後の承認者追加は管理者が
`finance.approval.policy_set` で行います。承認ポリシーがない間、発行・送付は
実行できません。

## 自然言語の請求フロー

例えば「訪問薬剤管理カレンダーの8月実績から請求書を作成して」と依頼した場合は、
次の順序で処理します。

1. Google Calendarの完全一致名 `訪問薬剤管理` を選択して同期する。
2. 顧客、契約、請求ルールを紐付ける。
3. 実際の開始・終了時刻を確認して訪問実績を完了する。予定時間は請求時間に使わない。
4. 実績を承認し、請求ランをプレビューする。
5. 対象を明示して請求ランを確定し、DRAFT請求書を生成する。
6. 内容を確認・承認して正式発行する。
7. 送付準備を別に承認し、Discord等へ送付する。
8. CSVまたは入金データを取り込み、消込候補を確認してから入金を割り当てる。

作成、承認、正式発行、外部送付は別操作です。「作成して」「確認して」だけでは
承認・送付へ進めません。Discord送付は `finance.delivery.prepare` の後に
`finance.delivery.dispatch_discord` を使用します。

## データ保護

顧客の氏名、住所、メールアドレス、口座情報、請求書PDF、カレンダーの生タイトルは
Hermes Memory、GBrain、通常ログへ保存しません。Finance DBとDocument Storeを
正本として使い、MCP応答も必要最小限の公開投影に限定します。

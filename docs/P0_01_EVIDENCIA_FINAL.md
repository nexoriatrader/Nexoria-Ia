# NEXORA IA — P0-01 — Evidências de isolamento e homologação sintética

**Escopo:** branch `feature/p0-01-isolamento-config-demo-real`, PR #45, base `audit/release-readiness-20261119`. Não executar operações financeiras, merge, deploy ou migração de produção. Revisão somente com mocks, dados sintéticos e PostgreSQL efêmero.

## Resultado da auditoria dos limites

A revisão encontrou inconsistência real na P0-01 inicial: `AccountRiskSettings.stop_loss` era salvo, porém `reserve_real_order` usava `RealRobotConsent.stop_loss`. Este último não era uma regra administrativa independente: `start_real_autopilot` o copiava do Stop Loss da conta selecionada na autorização. Duas cópias podiam divergir após alterações.

### Semântica verificada após correção

| Campo | Fonte operacional | Contrato |
| --- | --- | --- |
| Stake DEMO | `User.stake` e `RobotPlan` legado | Nunca é lido no worker REAL |
| Stop Win DEMO | `User.stop_win` | Guardas DEMO apenas |
| Stop Loss DEMO | `User.stop_loss` | Guardas DEMO apenas |
| Stake REAL | `AccountRiskSettings.stake` da conta selecionada | Consentimento possui `max_stake` como limite de autorização |
| Stop Win REAL | `AccountRiskSettings.stop_win` da conta selecionada | Aplicado por `real_ledger._risk_check` ao P/L REAL liquidado |
| Stop Loss REAL | `AccountRiskSettings.stop_loss` da conta selecionada | Aplicado por `real_ledger._risk_check` apenas se corresponder ao consentimento |
| Stop Loss do consentimento | `RealRobotConsent.stop_loss` | Cópia de autorização, nunca limite administrativo independente |
| Limite por entrada do consentimento | `RealRobotConsent.max_stake` | Teto capturado ao autorizar, verificado pelo ledger |

O código executa `validated_real_stop_loss(settings, consent)` em `app/real_ledger.py` e no `app/real_runtime.py`: se valores finitos e positivos não coincidirem, o processo é **fail-closed**, sem escolher silenciosamente o maior ou o menor limite. Quando coincidem, `reserve_real_order` envia **o valor vindo de AccountRiskSettings** para `_risk_check`. O worker interrompe a sessão antes de analisar sinais caso detecte divergência; o ledger rejeita a reserva mesmo que outro chamador tente acessar a função.

O início de sessão REAL relê stake e Stop Loss da conta antes de armazenar o consentimento; alteração concorrente detectada resulta em erro, sem persistência de autorização. Os dois controles administrativos de liberação permanecem inalterados e OFF por padrão.

## Cenários numéricos comprovados com dados fictícios

| DEMO | REAL | Consentimento | P/L hipotético | Resultado |
| --- | --- | --- | --- | --- |
| Stop Loss 900 | Stop Loss 3 | Stop Loss 8 | Nenhum | Bloqueio por divergência antes da reserva |
| Stop Loss 900 | Stop Loss 8 | Stop Loss 3 | Nenhum | Bloqueio por divergência antes da reserva |
| Stop Loss 100 | Stop Loss 7 | Stop Loss 7 | -6,50 + entrada 1,00 | Reserva recusada pelo Stop Loss REAL |
| Stop Win 0 | Stop Win 2 | Stop Loss 8 = REAL 8 | +2,20 | Reserva recusada pelo Stop Win REAL |
| Stop Loss 1 | Stop Loss 100 | Stop Loss 100 | Nenhum | DEMO bloqueia exposição 2; REAL não herda o stop DEMO |

Todos os resultados são simulados; não representam P/L real, aprovação de risco ou eficácia de estratégia.

## Testes automatizados

- `tests/test_real_ledger.py`: casos de discrepância, limites efetivos, separação da DEMO, recusa antes de reservar e worker recusando chamada simulada à corretora.
- `tests/test_mode_config_isolation.py`: controles por usuário/conta/modo, valores ausentes, preview de conta não salva com OAuth fictício, salvamento isolado, recuperação de preferências.
- `tests/test_mode_settings_ui_runtime.py`: executa funções JavaScript extraídas de `app/index.html` em Node, com DOM fictício, alterna DEMO ↔ duas REAL e descarta resposta atrasada da conta anterior.
- `tests/test_postgres_mode_isolation.py`: cria PostgreSQL localhost efêmero na ação GitHub, simula banco legado sem tabela nova, chama `db.init_db` duas vezes, verifica dados antigos, faz backup sintético da nova tabela, remove a tabela somente no banco temporário, valida legado intacto, recria a tabela e restaura registros.
- `.github/workflows/test.yml`: serviço PostgreSQL 16 descartável ligado somente à branch P0-01; outras etapas de CI preservadas.

O teste PostgreSQL exige `NEXORA_P0_EPHEMERAL_PG=yes` e um DSN `postgresql+psycopg` com host `127.0.0.1`/`localhost`, porta 5432, usuário `p0_ci` e banco `nexora_p0_ci`. Rejeita qualquer destino diferente, inclusive Supabase. Backout destrutivo da nova tabela só é válido **após exportação completa dos dados novos**; a produção não foi tocada.

## Observações e limites

1. Testes JavaScript são funcionais em Node com DOM sintético; não são uma auditoria manual em navegador real.
2. Os testes de SQLite e PostgreSQL descartável não provam impacto de migração no banco de produção, que não foi modificado.
3. O formulário de automação mantém as duas escritas existentes (perfil e risco) em operações separadas; uma transação única entre ambas pode ser considerada numa revisão de UX/consistência, sem confundir esse débito com o isolamento dos valores persistidos.
4. Estados UNKNOWN/PENDING e reinicialização permanecem reservados para P0-02/P0-04; não foram implementados nesta etapa.
5. Nenhum adaptador de compra foi habilitado ou chamado; validações de risco aqui não constituem autorização para negociações com dinheiro real.

**Aceitação recomendada:** registrar logs do CI no commit final, inspecionar o diff da PR #45, manter PR como rascunho e preservar a branch `main` sem merge.

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

1. Os testes JavaScript em Node continuam válidos; foi adicionada inspeção visual em navegador Chromium real (sem login, usando contas e APIs sintéticas), em desktop e celular. Não equivale a homologação em navegador conectado à produção.
2. Os testes de SQLite e PostgreSQL descartável não provam impacto de migração no banco de produção, que não foi modificado.
3. O assistente de automação agora usa gravação atômica do perfil e dos limites. As rotas legadas de gravação separada foram preservadas para compatibilidade; clientes externos que continuem combinando ambas ainda precisam migrar para a nova rota atômica.
4. Estados UNKNOWN/PENDING e reinicialização permanecem reservados para P0-02/P0-04; não foram implementados nesta etapa.
5. Nenhum adaptador de compra foi habilitado ou chamado; validações de risco aqui não constituem autorização para negociações com dinheiro real.

**Aceitação recomendada:** registrar logs do CI no commit final, inspecionar o diff da PR #45, manter PR como rascunho e preservar a branch `main` sem merge.


## Verificação adicional de encerramento — 19/11/2026

### 1. Inspeção visual manual em navegador

**Procedimento realizado:** `tests/browser_visual_p001.py` abre o HTML da própria branch em Chromium/Playwright através de servidor temporário em `127.0.0.1` com mapeamento dos arquivos `/brand/`. Toda API é interceptada por respostas sintéticas e as requisições externas são bloqueadas. O teste navega na interface existente, aciona os seletores reais da UI e registra capturas de tela em desktop (1060×770) e celular (390×844); as capturas foram **inspecionadas visualmente** na revisão.

**Evidência da seleção e dos valores apresentados:**

| Modalidade fictícia | Stake | Stop Win | Stop Loss | Resultado |
| --- | ---: | ---: | ---: | --- |
| REAL CRONE | 7,00 | 19,00 | 14,00 | Campos da conta CRONE exibidos |
| REAL CRTWO | 3,00 | 9,00 | 6,00 | Campos atualizados ao alternar conta |
| DEMO | 2,50 | 12,00 | 8,00 | Preferências DEMO restauradas, seletor REAL ocultado |

A alternância em desktop e a apresentação responsiva no celular não mostraram erros de valor ou campos de limite sobrepostos na área inspecionada. O teste também confirma que não são enviados POSTs nem ordens; o navegador não acessa Deriv, Render ou o banco de produção. A logomarca, inicialmente ausente na primeira execução por ser servida por rota FastAPI não simulada, foi corrigida no **harness local** mapeando `/brand/` para os arquivos existentes, sem alterar a aplicação.

**Ressalva:** são imagens e interações feitas com a interface real, mas contas/retornos de API simulados; falta homologação com sessão autenticada real em ambiente controlado, exclusivamente de leitura, caso seja exigida.

### 2. Inconsistência do salvamento e correção aplicada

**Diagnóstico confirmado:** `saveAutomationProfile()` fazia `POST /api/automation/profile` e só após sucesso enviava `POST /api/onboarding/config`. Cada handler gravava separadamente no banco. Falha da segunda requisição poderia deixar perfil novo com risco antigo ou ausente.

**Correção em branch:** adicionada `POST /api/automation/configuration`, com corpo `{profile,risk}`, reaproveitando as mesmas validações de perfil, seleção/posse da conta autenticada, catálogo, stake e progressão. O frontend envia **uma única solicitação**. O backend usa **uma única sessão SQLAlchemy e uma confirmação** para `AutomationProfile`, `AccountRiskSettings` e os espelhos legados DEMO quando aplicáveis. Qualquer erro gera `rollback()`, mantendo a configuração anterior. O endpoint apenas salva dados, não concede consentimento de execução e não compra contratos. Os endpoints legados continuam intactos para preservar compatibilidade.

**Evidências:** `tests/test_atomic_profile_settings.py` cobre sucesso, falha de validação, erro injetado **após staging/flush do perfil e antes do commit**, ausência de dados parcialmente persistidos, robô ligado, conta REAL autorizada e não autorizada fictícias e ausência de chamadas à corretora.

**Limite de compatibilidade:** clientes externos que chamam as APIs legadas em separado ainda poderão criar estados parcialmente salvos. A integração oficial do configurador ficou atômica; recomendar migração gradual dos consumidores legados e manutenção das verificações que recusam parâmetros REAL ausentes ou divergentes.

### 3. Duas proteções administrativas independentes

`app/real_runtime.py::real_available()` mantém a operação lógica **E** sobre `NEXORA_REAL_AUTOPILOT_ENABLED` e `NEXORA_REAL_RELEASE_APPROVED`, ambas `false` na ausência de configuração. Salvar configurações no novo endpoint não altera flags, consentimentos nem o estado do worker. `tests/test_release_modes.py` valida comportamento com uma só flag ligada; os jobs de CI rodam com ambas desativadas ou padrão OFF.

A configuração **efetiva do serviço Render** não foi lida nesta revisão, por não existir workspace confirmado nesta sessão; portanto a conclusão é uma **verificação dos guardas em código e testes**, não uma afirmação de valores atuais do ambiente hospedado.

### 4. Critérios de encerramento e preservação

- Inspeção das capturas reais do Chromium e alternância entre contas sintéticas: **concluída**.
- Falha parcial de gravação no configurador principal: **corrigida**; teste de rollback: **concluído**.
- Gates financeiros de código e valores padrão: **preservados**; sem confirmação de flags efetivas Render.
- PostgreSQL efêmero, restore e reversão do esquema: **concluídos anteriormente, sem Supabase**.
- Nenhuma migração de produção, merge, deploy, OTP, proposta de compra ou contrato Deriv: **realizado = nenhum**.
- P0-02: **não iniciada**.

Ver o status atualizado no [CI da branch P0-01](https://github.com/nexoriatrader/Nexoria-Ia/actions/workflows/test.yml) e na [PR #45](https://github.com/nexoriatrader/Nexoria-Ia/pull/45). O SHA final e a contagem dos testes são registrados no relatório de entrega, após conclusão do último CI.

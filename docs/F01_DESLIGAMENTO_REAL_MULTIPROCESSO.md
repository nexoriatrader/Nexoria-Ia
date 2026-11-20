# F01 — Desligamento REAL persistido e independente do processo

**Escopo:** exclusivamente a F01 da auditoria P0-01–P0-05.

**Branch:** `fix/f01-real-stop-cross-process` criada do SHA exato da P0-05, `81b52803b954ab9a7831be05c9b7e98501968e60` (PR #49).

**Restrições:** nenhuma implantação, merge, migração ou alteração no Supabase de produção; sem mudanças nos gates administrativos `NEXORA_REAL_AUTOPILOT_ENABLED` / `NEXORA_REAL_RELEASE_APPROVED`; sem envio de ordens à Deriv; nenhuma alteração em `app/real_broker.py`, `app/restart_consistency.py` ou ledgers. Foi adicionada **somente na branch** a tabela de geração F01; sua migração de produção continua pendente e proibida nesta entrega. Todos os cenários financeiros são substituídos por dados sintéticos. F02–F07 **não iniciadas**.

## Diagnóstico confirmado na base P0-05

A rota `POST /api/autopilot/stop` dependia de `user.id in real_runtime.running`, um `set` de memória local ao worker, para decidir se revogaria o consentimento REAL persistido. Em Render com vários processos, o pedido poderia chegar a B enquanto A mantinha o worker. B executava apenas o caminho DEMO e não gravava `RealRobotConsent.revoked_at`; portanto a última guarda de envio da P0-04 continuava encontrando consentimento válido em A.

A F01 corrige a decisão de revogação, **não** a autorização para operações financeiras.

## Arquivos alterados

| Arquivo | Alteração |
| --- | --- |
| `app/db.py` | **Nova tabela apenas na branch** `real_authorization_fences` com `user_id` PK/FK, `generation` inteira e `updated_at`. Migração futura necessária; nenhuma produção alterada. |
| `app/real_runtime.py` | Captura e validação transacional de geração persistida para START; STOP invalida gerações anteriores e revoga o consentimento por usuário/conta com `SELECT FOR UPDATE`. As transações têm `lock_timeout=1500ms` e `statement_timeout=4500ms` em PostgreSQL, com rollback em falha; nenhum broker ou contrato é alterado. |
| `app/main.py` | START captura geração antes da primeira verificação externa e só confirma consentimento se a geração for a atual, sob a **mesma transação**. STOP executa revogação e limpeza DEMO separadamente em threads, preserva os campos antigos e distingue falhas parciais; erro de revogação retorna 503, limpeza DEMO após revogação confirmada retorna 207. |
| `tests/test_f01_real_stop.py` | **Novo:** testes HTTP sintéticos de autenticação, isolamento, ausência/revogação prévia, geração mesmo quando consentimento falta, compatibilidade DEMO, classificação REAL/DEMO, falha parcial e resposta do loop ASGI durante atraso simulado no PostgreSQL. |
| `tests/test_postgres_f01_real_stop.py` | **Novo:** processos independentes comprovam STOP após START antigo, novo START após STOP, START finalizando dentro de transação antes do STOP, timeout/rollback sob contenção, preservação de contratos e isolamento. Todos os sinais/ordens são fictícios, sem Deriv. |
| `.github/workflows/test.yml` | Inclui a branch isolada, mantém ambos os gates REAL em `false`, provisiona somente PostgreSQL 16 local descartável e chave de autenticação fictícia temporária para as requisições HTTP entre processos. |

## Contrato da rota: compatibilidade + semântica verificável

Campos legados `running`, `execution` e `real_orders_enabled` continuam disponíveis. Os campos novos são **aditivos**:

- `real_revocation_status`: `REVOKED_NOW`, `ALREADY_REVOKED` ou `NO_CONSENT`.
- `real_revocation_persisted`: `true` quando existe consentimento com revogação confirmada; `null` quando não existe linha de consentimento a revogar. Não simula gravação quando a linha não existe.
- `real_authorization_absent: true`: a revogação foi confirmada na transação do STOP. Isso **não** autoriza afirmar ausência futura se o usuário fizer um novo START explicitamente depois.
- `running_scope: LOCAL_PROCESS_ONLY`, `running_now: null`, `worker_shutdown_confirmed: null`, `worker_shutdown_state: UNKNOWN_REMOTE_WORKERS`. O resultado não afirma que outro processo encerrou imediatamente.
- Em erro na persistência, a rota devolve **503**, não `running:false`. Os dados em memória não são descartados antecipadamente como se a revogação tivesse sido confirmada.

O desligamento limpa o DEMO do mesmo usuário de forma independente, conservando `AutoPilotResumeConsent` e `RobotRun` dos demais usuários. Uma conta REAL de outro usuário permanece intocada. Uma conta diferente do consentimento atual não é selecionada nem modificada. Registros `PENDING`, `UNKNOWN` e `CONFIRMED` conservam estado, slot, chave, identificador e resultado.

## Ordem transacional e relação com P0-04

O stop usa `SELECT FOR UPDATE` na **mesma linha de `RealRobotConsent`** utilizada pelo guard final `locked_real_consent_for_send` da P0-04. Se a revogação já foi confirmada, a validação posterior não pode permitir novo envio. Se um guard já estiver aberto antes do stop, a revogação espera sua liberação; não pode ser declarada concluída no meio da janela protegida.

**Limitação imprescindível:** uma solicitação externa iniciada antes da revogação confirmada não pode ser desfeita. O bloqueio PostgreSQL durante uma chamada externa pode atrasar o stop; a avaliação dos limites de duração e coordenação mais ampla pertence ao achado F04. **F01 não modifica `restart_consistency.py`.**

O status de outros workers continuará desconhecido até existir mecanismo de evidência distribuída; não se deve interpretar `running:false` legado como verificação global. A F01 não cria heartbeat, canal de compra nem ferramentas de envio financeiro. O mecanismo de START existente permanece sujeito aos gates atuais e agora exige fencing persistido.

## Matriz mínima de evidências sintéticas

1. Stop com consentimento ativo e sem flag `running` local; revogação persiste para o usuário autenticado.
2. Outro usuário/conta mantém consentimento e ordens inalterados.
3. Stop repetido devolve `ALREADY_REVOKED` e conserva o timestamp de revogação, mas **avança a geração de fencing** para invalidar STARTs antigos ainda pendentes.
4. Sem consentimento REAL devolve `NO_CONSENT`, conserva desligamento DEMO e não cria autorização.
5. Requisição anônima é recusada sem tocar em qualquer consentimento.
6. Falha simulada do banco devolve 503, sem afirmar parada global.
7. Processo A prepara `PENDING`, B recebe stop REAL por HTTP e revoga; após commit A prossegue e **não chama o adaptador simulado**.
8. Processos independentes comprovam serialização do guard de envio existente e do stop, com marcadores sintéticos sem rede.
9. Histórico com `PENDING`, `UNKNOWN` e `CONFIRMED` permanece íntegro; suíte geral e demais casos PostgreSQL herdados continuam passando.

## Registro de execução e pendências

- **CI da implementação e testes:** commit `0edf783cfe501e7ef06d227b703246571d984041`, [GitHub Actions #44638957726](https://github.com/nexoriatrader/Nexoria-Ia/actions/runs/44638957726), **SUCCESS**: 441 testes gerais aprovados, 18 omitidos, 1 aviso; 1 teste adicional de carga somente leitura aprovado (10 usuários sintéticos / 90 requisições); 18 testes PostgreSQL 16 descartáveis aprovados, incluindo dois testes F01 multiprocesso. Chromium omitido: nenhuma alteração de interface.
- **CI do primeiro relatório:** commit `a075e608a77439bbb2ffc36490c4b63de2adc5ec`, [GitHub Actions #44639159730](https://github.com/nexoriatrader/Nexoria-Ia/actions/runs/44639159730), **SUCCESS**, com as mesmas contagens de regressão. Este documento poderá receber um SHA posterior de revisão de evidências, que deve ter seu próprio CI confirmado separadamente.
- **PR:** [#50](https://github.com/nexoriatrader/Nexoria-Ia/pull/50) em rascunho, base da P0-05; não alterar `main`.
- **Riscos ainda abertos:** F02–F07 não abordados; flags REAL do Render não verificadas no workspace autorizado; clientes externos das APIs antigas ainda não inventariados; execução distribuída não comprova encerramento instantâneo dos workers; bloqueios DB durante requisições já iniciadas permanecem sujeitos a F04.

**Decisão desta etapa:** implementação e testes da F01 sujeita à revisão independente. Não é autorização para merge, deploy, execução REAL ou avanço para F02.


## Revisão F01 — quatro achados corrigidos e limites de aceitação

### R1 — START × STOP com geração persistida

A tabela `real_authorization_fences` mantém uma geração inteira por `user_id`. START captura a geração antes de verificações externas; essa captura **não autoriza uma operação**. STOP adquire `SELECT FOR UPDATE` na linha do usuário (também serializa criação inicial da linha de fencing), depois na linha de geração e no consentimento, incrementa geração e revoga, tudo **no mesmo commit**. Isso ocorre mesmo com consentimento inexistente ou já revogado, pois um START antigo pode permanecer pendente nesses casos.

Na etapa final de START, a verificação da geração esperada, o novo exame de configuração/limites e a escrita de `RealRobotConsent` ocorrem na **mesma transação**, sob os mesmos bloqueios em ordem `User → RealAuthorizationFence → RealRobotConsent`. Uma geração antiga resulta em `RealStartInvalidated` / HTTP **409**, sem reativar o consentimento. Não é suficiente checar a geração em uma transação e autorizar em outra; a implementação não faz isso.

A ordenação é definida pelo **registro persistido da tentativa START**, não por timestamps arbitrários em processos de aplicação. Se a intenção START ainda não tiver capturado a geração no banco, não pode ser considerada autorização. A resolução de corridas de edit/configuração fora desse escopo permanece em F03.

### R2 — DEMO não é rotulado REAL por consentimento histórico

A resposta de `POST /api/autopilot/stop` não usa mais a simples existência de um `account_id` histórico para rotular `execution`. Considera o worker REAL local e a modalidade do perfil salva. Perfil DEMO com consentimento REAL antigo já revogado continua `execution=DERIV_DEMO_ONLY`, ao mesmo tempo que a resposta identifica `real_revocation_status=ALREADY_REVOKED`.

### R3 — timeout PostgreSQL e evento assíncrono

As transações START/STOP utilizam `SET LOCAL lock_timeout = '1500ms'` e `SET LOCAL statement_timeout = '4500ms'` somente no PostgreSQL. A rota START e a rota STOP delegam as operações de persistência bloqueantes a `asyncio.to_thread`, de modo a não bloquear o loop ASGI durante espera de I/O.

Timeout/falha de contenção retorna **503** sem declaração de revogação confirmada e com rollback. A contenção do lock final antes de envio permanece uma limitação operacional da P0-04/F04: ordens iniciadas antes do STOP confirmado não podem ser desfeitas.

### R4 — revogação REAL e limpeza DEMO independentes

A etapa de revogação REAL confirma o seu próprio commit e **só então** a limpeza DEMO é tentada. Se a segunda etapa falhar, a API devolve **HTTP 207 (resultado parcial)** com `real_revocation_persisted=true`, `demo_cleanup_status=FAILED`, `partial_failure=true` e `running=null`; o resultado REAL confirmado não é omitido. Caso ambas as etapas sejam verificadas, permanece HTTP 200 e `demo_cleanup_status=COMPLETED`. Em falha da primeira etapa, retorna 503 e não afirma sucesso.

Os campos legados continuam disponíveis em respostas bem-sucedidas; consumidores antigos precisam ser avaliados quanto ao tratamento de HTTP 207 e `running=null` em falha parcial. Essa é uma limitação de compatibilidade explicitamente pendente.

## Evidência multiprocesso adicional

- `test_f01_old_start_cannot_restore_consent_after_stop_commits`: processo A captura geração e pausa; processo B executa STOP HTTP e confirma; A tenta finalizar e recebe `invalidated`. Nenhum consentimento reativado; outros usuários e contratos preservados.
- `test_f01_stop_before_new_start_requires_new_generation`: processo B conclui STOP; novo START sintético captura geração posterior e finaliza somente com ela; outro STOP revoga novamente. Não executa API Deriv nem libera flags.
- `test_f01_start_transaction_finishes_before_concurrent_stop_then_stop_wins`: START mantém a trava após verificar geração; STOP concorrente espera; após o commit START, STOP revoga. O consentimento termina revogado.
- `test_f01_pg_lock_timeout_fails_without_confirming_stop`: um processo retém a linha `User`, outro tenta STOP. O timeout produz 503 e rollback; o consentimento e a geração permanecem intactos.
- `test_slow_revocation_does_not_block_async_health_endpoint`: aguardo de banco simulado em thread enquanto `GET /healthz` continua respondendo no mesmo loop.
- Testes HTTP de DEMO histórico revogado, `NO_CONSENT` com token antigo pendente, repetição e falha parcial após revogação já confirmada.

## Nova tabela: migração futura obrigatória e ainda NÃO autorizada

**Esquema lógico:** `real_authorization_fences(user_id INTEGER PRIMARY KEY REFERENCES users(id), generation INTEGER NOT NULL DEFAULT 0, updated_at TIMESTAMPTZ NOT NULL)`. O modelo foi adicionado em `app/db.py` **somente na branch F01**; os testes utilizam `db.Base.metadata.create_all` exclusivamente em SQLite e PostgreSQL 16 descartáveis.

**Pendência impeditiva para implantação:** preparar e revisar uma migração aditiva e reversível da tabela para um ambiente **de homologação explicitamente autorizado**, verificar privilégios, índices, criação durante tráfego e backup. **Nenhuma migração foi executada no Supabase de produção.** Como `db.init_db()` no repositório atualmente utiliza `create_all`, uma futura revisão de deploy também deve impedir DDL implícito em produção antes da migração autorizada. Enquanto a tabela não existir, START/STOP que dependem do fencing **falham fechado**, não utilizam consentimento sem coordenação.

O backout precisa ser definido separadamente: remover o código de fencing ou a tabela sem avaliar STARTs pendentes poderia reintroduzir a corrida. Não executar rollback ou migração real com esta PR isolada.

## Limites que permanecem

- **Sem garantia de encerramento físico instantâneo dos workers remotos.** `worker_shutdown_confirmed=null` continua correto.
- Uma requisição já enviada ao adaptador antes de um STOP confirmado não pode ser desfeita. O guard P0-04 continua indispensável.
- Uma revogação que ocorra por outro fluxo que não avance geração (por exemplo, reinicialização pela P0-04) exige análise separada de coordenação. Esta F01 valida e protege START × STOP via rota prevista, sem alterar `app/restart_consistency.py`.
- F02–F07 continuam pendentes. CI verde não representa aprovação de implantação nem revisão externa independente.

## Evidências atualizadas de CI

- [GitHub Actions #44645342766](https://github.com/nexoriatrader/Nexoria-Ia/actions/runs/44645342766), código e testes até `4f5a329b3e8adb14af9b4dd34c4734164a658562`: **447 gerais aprovados, 22 omitidos, 2 avisos; 22 PostgreSQL descartáveis aprovados; 1 teste adicional de carga somente leitura aprovado**. O aviso adicional da suíte geral era uma depreciação no teste de cookies, posteriormente corrigida em commit exclusivo de testes.
- O commit documental final posterior e o seu CI devem ser verificados separadamente antes de encerrar a entrega.

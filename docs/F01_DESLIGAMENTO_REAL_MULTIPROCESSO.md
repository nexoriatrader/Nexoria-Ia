# F01 — Desligamento REAL persistido e independente do processo

**Escopo:** exclusivamente a F01 da auditoria P0-01–P0-05.

**Branch:** `fix/f01-real-stop-cross-process` criada do SHA exato da P0-05, `81b52803b954ab9a7831be05c9b7e98501968e60` (PR #49).

**Restrições:** nenhuma implantação, merge, migração ou alteração no Supabase de produção; sem mudanças nos gates administrativos `NEXORA_REAL_AUTOPILOT_ENABLED` / `NEXORA_REAL_RELEASE_APPROVED`; sem envio de ordens à Deriv; nenhuma alteração em `app/real_broker.py` ou ledgers. **`app/restart_consistency.py` foi alterado exclusivamente para integrar o fencing global ao reinício.** Foi adicionada **somente na branch** a tabela de geração F01; sua migração de produção continua pendente e proibida nesta entrega. Todos os cenários financeiros são substituídos por dados sintéticos. F02–F07 **não iniciadas**.

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

**Limitação imprescindível:** uma solicitação externa iniciada antes da revogação confirmada não pode ser desfeita. O bloqueio PostgreSQL durante uma chamada externa pode atrasar o stop; a avaliação dos limites de duração e coordenação mais ampla pertence ao achado F04. O reinício P0-04 agora incrementa a geração global no mesmo commit que revoga os consentimentos.

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

Na etapa final de START, a verificação **das duas gerações** esperadas, o novo exame de configuração/limites e a escrita de `RealRobotConsent` ocorrem na **mesma transação**, sob os bloqueios na ordem `RealGlobalAuthorizationFence (SHARE) → User → RealAuthorizationFence → RealRobotConsent`. Uma geração antiga resulta em `RealStartInvalidated` / HTTP **409**, sem reativar o consentimento. Não é suficiente checar a geração em uma transação e autorizar em outra; a implementação não faz isso.

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

**Pendência impeditiva para implantação:** preparar e revisar uma migração aditiva e reversível da tabela para um ambiente **de homologação explicitamente autorizado**, verificar privilégios, índices, criação durante tráfego e backup. **Nenhuma migração foi executada no Supabase de produção.** A nova versão de `db.init_db()` não executa `create_all()` nem `ALTER TABLE` no PostgreSQL: valida esquema e revisão apenas com consultas de leitura e recusa iniciar caso faltem estruturas da migração. Enquanto a tabela não existir, START/STOP que dependem do fencing **falham fechado**, não utilizam consentimento sem coordenação.

O backout precisa ser definido separadamente: remover o código de fencing ou a tabela sem avaliar STARTs pendentes poderia reintroduzir a corrida. Não executar rollback ou migração real com esta PR isolada.

## Limites que permanecem

- **Sem garantia de encerramento físico instantâneo dos workers remotos.** `worker_shutdown_confirmed=null` continua correto.
- Uma requisição já enviada ao adaptador antes de um STOP confirmado não pode ser desfeita. O guard P0-04 continua indispensável.
- Reinicialização P0-04, logout, STOP, desligamento de segurança e suspensão administrativa agora invalidam o START antigo antes de confirmar o commit; essa integração exige nova revisão independente da sequência de bloqueios e implantação somente após migração formal.
- F02–F07 continuam pendentes. CI verde não representa aprovação de implantação nem revisão externa independente.

## Evidências atualizadas de CI

- [GitHub Actions #44645342766](https://github.com/nexoriatrader/Nexoria-Ia/actions/runs/44645342766), código e testes até `4f5a329b3e8adb14af9b4dd34c4734164a658562`: **447 gerais aprovados, 22 omitidos, 2 avisos; 22 PostgreSQL descartáveis aprovados; 1 teste adicional de carga somente leitura aprovado**. O aviso adicional da suíte geral era uma depreciação no teste de cookies, posteriormente corrigida em commit exclusivo de testes.
- O commit documental final posterior e o seu CI devem ser verificados separadamente antes de encerrar a entrega.

## Revisão adicional de segurança — fencing em TODOS os caminhos de revogação

### Inventário auditado dos caminhos de escrita em REAL

| Evento | Transação e invalidação de START antigo | Evidência necessária |
| --- | --- | --- |
| STOP manual `/api/autopilot/stop` | `revoke_real_consent_for_stop` usa a função comum, com global em `SHARE`, `User`, geração do usuário e consentimento, e um commit | Testes F01 STOP × START e STOP × envio fictício |
| Reinicialização `prepare_service_restart()` | `revoke_stale_real_consent_after_restart` adquire global `FOR UPDATE`, **avança geração global mesmo sem consentimento** e executa `UPDATE` coletivo de revogação no **mesmo commit** | Testes multiprocesso com e sem consentimento, duas reinicializações, preservação de PENDING/UNKNOWN/CONFIRMED |
| Desligamento de segurança `disarm_real(uid,reason)` | Delega **exclusivamente** a `revoke_real_consent_for_stop`, que avança a geração individual antes do commit; remove estado local em `finally`, inclusive em erro DB, sem declarar revogação persistida falsa | Teste `safety` em outro processo concorrendo com START antigo |
| Logout `/api/logout` | `_f01_logout_user_session` revoga com `revoke_real_consent_in_session` **na mesma transação** que remove a sessão e os registros de retomada DEMO | Teste de logout HTTP concorrente; sessão não é apagada se revogação falhar |
| Administração `/api/admin/access/adjust` | `suspend`, `refund_revoke` e `remove_hours` quando o acesso deixa de ser permitido invalidam a geração individual e revogam consentimento junto com a alteração de `RobotAccess` e o evento de auditoria, em um só commit | Teste de suspensão sintética via HTTP e concorrência com START |
| `disarm_revoked_real_workers()` | **Somente observa** revogação já persistida e remove flags locais; não escreve `revoked_at` | Regressão P0-04 de leitura da revogação |
| `/api/admin/logout` | Apenas invalida sessão do administrador, **não** é revogação de usuário ou autorização REAL | Não cria consentimentos nem altera gerações |

**Varredura de código:** as únicas escritas de `RealRobotConsent.revoked_at` autorizadas são (a) confirmação de START validada sob geração global+individual na função `finalize_real_start_if_current`, (b) revogação comum em `real_runtime` e (c) `UPDATE` coletivo no reinício com geração global exclusiva. Não existe outro caminho ativo de revogação direta em `main.py`, `real_runtime.py` ou demais módulos de aplicação auditados.

### Bloqueios, ordem e compatibilidade P0-04

**Regra de ordem:** `global epoch → User → user fence → REAL consent`. Per-user START/STOP/logout/admin/safety usam `SELECT ... FOR SHARE` no global e `FOR UPDATE` em `User`/fence/consent. Duas ações de usuários diferentes podem compartilhar o bloqueio global; o reinício usa `SELECT ... FOR UPDATE` no global e bloqueia novos STARTs até que a varredura global seja confirmada.

A varredura de reinício executa `UPDATE` coletivo somente após obter o bloqueio **exclusivo** global; como START/STOP precisam da guarda global antes de tocar qualquer consentimento, ela não precisa bloquear cada `User` durante o sweep para impedir reativação. O guard final da P0-04 permanece **inalterado**, adquirindo somente o consentimento `FOR UPDATE` durante a chamada existente; jamais solicita a guarda global depois de obter a linha, evitando inversão `consent → global`. Contratos e slots não são alterados. O PostgreSQL aplica `lock_timeout=1500ms` e `statement_timeout=4500ms` às transações de revogação da F01.

Uma falha de persistência causa rollback e **não permite** anunciar revogação confirmada. A inicialização aborta se a revogação global falhar; nenhuma autorização REAL é restaurada automaticamente.

### Inicialização PostgreSQL SEM DDL

`app/db.py::init_db()` diferencia explicitamente mecanismos:

- **PostgreSQL (aplicação):** `verify_postgresql_schema()` usa somente reflexão SQLAlchemy e `SELECT`, exige todas as tabelas obrigatórias, colunas das estruturas de fencing, a revisão `f01_global_fencing_v1` em `nexora_schema_revisions` e a linha `id=1` com geração global válida. Falta de qualquer elemento aborta a inicialização antes de criar workers. Não executa `Base.metadata.create_all`, `ALTER TABLE`, `INSERT` nem atualização de revisão.
- **SQLite local:** fluxo de desenvolvimento legado de criação automática permanece para compatibilidade. PostgreSQL descartável é provisionado **explicitamente pelos testes**, não pelo `init_db()`.
- **Proposta formal de migração (NÃO EXECUTADA):** [`migrations/F01_GLOBAL_FENCING_V1_POSTGRES.sql`](../migrations/F01_GLOBAL_FENCING_V1_POSTGRES.sql) documenta criação/validação das tabelas de gerações global/individual, linha singleton global e marcador de revisão. A execução exige DBA e autorização futura separados, homologação, janela de manutenção, backup e plano próprio de rollback. O arquivo não é chamado por CI, startup nem aplicação.

O teste `tests/test_postgres_f01_schema_guard.py` inspeciona as instruções enviadas durante `db.init_db()` em PostgreSQL **descartável** isolado, exigindo **zero DDL** em esquema ausente/incompleto e em esquema válido. Também confirma a recusa de versão de migração ou linha global ausentes, sem criação implícita.

**Aviso de compatibilidade:** o teste legado de migração da P0-01 foi adaptado para criar a tabela de risco somente por fixture explícita em PostgreSQL descartável; o produto não poderá voltar a executar DDL automático na inicialização. A nova versão exige migração formal e pode manter o serviço indisponível enquanto a migração não for aprovada.

### Matriz adicional de testes

- `test_f01_old_start_invalidated_by_every_persisted_revocation_path[restart,safety,logout,admin]`: START sintético em processo A captura geração, cada revogador age em outro processo B; A não pode finalizar com o token antigo; os contratos persistidos permanecem intactos.
- `test_f01_restart_invalidates_old_start_without_any_consent_row`: sweep global inválida até a tentativa START que não possuía consentimento.
- `test_f01_new_start_after_restart_captures_new_global_epoch`: novo START sintético após revogação global usa geração nova (somente operação SQL fictícia; nenhum broker).
- `test_two_independent_postgres_restart_processes_revoke_once`: reinícios concorrentes serializados com count de revogação idempotente, sem deadlock e preservando ledger.
- `test_postgresql_init_db_is_strictly_read_only_and_requires_formal_revision`: zero DDL e DML de startup PostgreSQL; esquema ausente, marca de migração ausente, marca válida e linha singleton ausente.
- `test_postgresql_migration_version_missing_blocks_all_automatic_ddl`: nenhum objeto criado por duas tentativas de inicialização sem migração.

**Esses cenários usam apenas processos de teste, dados sintéticos, PostgreSQL local descartável e broker não acessível.** A aprovação de CI não substitui uma nova revisão humana independente da PR.


## F01 — Fechamento da validação estrutural da migração (sem implantação)

Correção restrita ao risco de `CREATE TABLE IF NOT EXISTS` sobre tabela preexistente
incompatível: `migrations/F01_GLOBAL_FENCING_V1_POSTGRES.sql` mantém um único
`BEGIN/COMMIT`, bloqueia as tabelas F01 durante a validação, examina objetos
no `current_schema()` e **somente depois** insere a linha singleton e a revisão
`f01_global_fencing_v1`. Em caso de incompatibilidade, `RAISE EXCEPTION`
aborta a transação; nenhum objeto recém-criado nem carimbo novo sobrevivem.
A execução repetida valida novamente e preserva as gerações já avançadas.
O pré-requisito `users(id INTEGER PRIMARY KEY)` é inspecionado.

Contrato exigido para cada uma das três tabelas F01: tabela comum, conjunto
exato de colunas, tipos PostgreSQL, `NOT NULL`, defaults, chave primária e
seu índice íntegro, CHECKs validados e obrigatórios (geração não negativa,
ID global igual a 1), FK de usuário referenciando `users(id)` no schema
esperado, sem `ON DELETE CASCADE`, sem constraints não validadas ou extras,
e singleton global com geração válida. Nomes de constraints podem variar,
mas suas definições não. A tabela de revisões também é validada antes do
registro. Não há reconstrução automática de estruturas preexistentes
incorretas.

`app/db.py` executa uma inspeção `SELECT` do mesmo contrato no startup
PostgreSQL, inclusive quando alguém preencheu manualmente a revisão sobre um
esquema incompatível: o serviço falha fechado. Os metadados das duas tabelas
de fencing incluem defaults de servidor e CHECKs equivalentes para fixtures
descartáveis; isso **não ativa DDL** em inicialização PostgreSQL.

Testes exclusivos PostgreSQL 16 descartável:
`tests/test_postgres_f01_migration_validation.py` cobre criação com
estruturas ausentes, estruturas preexistentes válidas, reexecução sem reset
de geração, incompatibilidades de colunas/tipos/timestamp/nullability/
default/PK/FK/CHECK/singleton, rollback sem novo carimbo, revisão mal
tipada e recusa do startup de esquema incompatível mesmo carimbado.
`tests/test_postgres_f01_schema_guard.py` segue exigindo zero DDL/DML
no startup. Ambos são chamados por `.github/workflows/test.yml` na branch F01.

**Limites preservados:** a migração acima é somente uma proposta versionada;
não foi executada em Supabase/produção. Nenhuma mudança nos gates
administrativos REAL, nenhum envio Deriv, merge, deploy ou início de F02.
A PR #50 permanece DRAFT. O encerramento técnico da F01 depende de CI
verde no SHA final e revisão independente consolidada P0-01 a P0-05 + F01.

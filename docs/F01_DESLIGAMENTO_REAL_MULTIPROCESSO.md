# F01 — Desligamento REAL persistido e independente do processo

**Escopo:** exclusivamente a F01 da auditoria P0-01–P0-05.

**Branch:** `fix/f01-real-stop-cross-process` criada do SHA exato da P0-05, `81b52803b954ab9a7831be05c9b7e98501968e60` (PR #49).

**Restrições:** nenhuma implantação, merge, migração ou alteração no Supabase de produção; sem mudanças nos gates administrativos `NEXORA_REAL_AUTOPILOT_ENABLED` / `NEXORA_REAL_RELEASE_APPROVED`; sem envio de ordens à Deriv; nenhuma alteração em `app/real_broker.py`, `app/restart_consistency.py`, ledgers ou esquema. Todos os cenários financeiros são substituídos por dados sintéticos. F02–F07 **não iniciadas**.

## Diagnóstico confirmado na base P0-05

A rota `POST /api/autopilot/stop` dependia de `user.id in real_runtime.running`, um `set` de memória local ao worker, para decidir se revogaria o consentimento REAL persistido. Em Render com vários processos, o pedido poderia chegar a B enquanto A mantinha o worker. B executava apenas o caminho DEMO e não gravava `RealRobotConsent.revoked_at`; portanto a última guarda de envio da P0-04 continuava encontrando consentimento válido em A.

A F01 corrige a decisão de revogação, **não** a autorização para operações financeiras.

## Arquivos alterados

| Arquivo | Alteração |
| --- | --- |
| `app/real_runtime.py` | Função `revoke_real_consent_for_stop(uid)` que procura a linha do consentimento pelo usuário autenticado e a bloqueia com `SELECT FOR UPDATE`. O `UPDATE` condiciona `user_id`, `account_id` da própria linha e `revoked_at IS NULL`. A confirmação exige commit; ausência e revogação prévia são retornos idempotentes distintos. A função não chama a corretora e não edita contratos. |
| `app/main.py` | `autopilot_stop` chama sempre a revogação persistida, independentemente de `running` local. Somente depois da confirmação/constatação segura limpa o estado REAL local; preserva o desligamento DEMO. Em falha de banco retorna 503, sem declarar sucesso. Campos aditivos distinguem consentimento revogado de workers remotos não confirmados. |
| `tests/test_f01_real_stop.py` | **Novo:** testes HTTP sintéticos de autenticação, escopo por usuário/conta, zero alteração em contratos, ausência de consentimento, consentimento já revogado, repetição idempotente, preservação de DEMO, erro do banco e ausência de chamadas ao broker. |
| `tests/test_postgres_f01_real_stop.py` | **Novo:** teste PostgreSQL descartável entre processos: worker faz reserva sintética `PENDING` e pausa, outro processo atende `POST /api/autopilot/stop`, revoga o consentimento e o worker retoma sem qualquer envio; teste adicional de serialização com bloqueio de linha concorrente e marcador fictício, sem rede. |
| `.github/workflows/test.yml` | Inclui a branch isolada, mantém ambos os gates REAL em `false`, provisiona somente PostgreSQL 16 local descartável e chave de autenticação fictícia temporária para as requisições HTTP entre processos. |

## Contrato da rota: compatibilidade + semântica verificável

Campos legados `running`, `execution` e `real_orders_enabled` continuam disponíveis. Os campos novos são **aditivos**:

- `real_revocation_status`: `REVOKED_NOW`, `ALREADY_REVOKED` ou `NO_CONSENT`.
- `real_revocation_persisted`: `true` quando existe consentimento com revogação confirmada; `null` quando não existe linha de consentimento a revogar. Não simula gravação quando a linha não existe.
- `real_authorization_absent: true`: nenhuma autorização REAL válida permanece para o usuário após a conclusão bem-sucedida.
- `running_scope: LOCAL_PROCESS_ONLY`, `running_now: null`, `worker_shutdown_confirmed: null`, `worker_shutdown_state: UNKNOWN_REMOTE_WORKERS`. O resultado não afirma que outro processo encerrou imediatamente.
- Em erro na persistência, a rota devolve **503**, não `running:false`. Os dados em memória não são descartados antecipadamente como se a revogação tivesse sido confirmada.

O desligamento limpa o DEMO do mesmo usuário de forma independente, conservando `AutoPilotResumeConsent` e `RobotRun` dos demais usuários. Uma conta REAL de outro usuário permanece intocada. Uma conta diferente do consentimento atual não é selecionada nem modificada. Registros `PENDING`, `UNKNOWN` e `CONFIRMED` conservam estado, slot, chave, identificador e resultado.

## Ordem transacional e relação com P0-04

O stop usa `SELECT FOR UPDATE` na **mesma linha de `RealRobotConsent`** utilizada pelo guard final `locked_real_consent_for_send` da P0-04. Se a revogação já foi confirmada, a validação posterior não pode permitir novo envio. Se um guard já estiver aberto antes do stop, a revogação espera sua liberação; não pode ser declarada concluída no meio da janela protegida.

**Limitação imprescindível:** uma solicitação externa iniciada antes da revogação confirmada não pode ser desfeita. O bloqueio PostgreSQL durante uma chamada externa pode atrasar o stop; a avaliação dos limites de duração e coordenação mais ampla pertence ao achado F04. **F01 não modifica `restart_consistency.py`.**

O status de outros workers continuará desconhecido até existir mecanismo de evidência distribuída, objeto de correções futuras; não se deve interpretar `running:false` legado como verificação global. A F01 não cria heartbeat, canal de compra, novos consentimentos, migradores ou ferramentas de envio financeiro.

## Matriz mínima de evidências sintéticas

1. Stop com consentimento ativo e sem flag `running` local; revogação persiste para o usuário autenticado.
2. Outro usuário/conta mantém consentimento e ordens inalterados.
3. Stop repetido devolve `ALREADY_REVOKED`, conserva timestamp e não escreve novamente.
4. Sem consentimento REAL devolve `NO_CONSENT`, conserva desligamento DEMO e não cria autorização.
5. Requisição anônima é recusada sem tocar em qualquer consentimento.
6. Falha simulada do banco devolve 503, sem afirmar parada global.
7. Processo A prepara `PENDING`, B recebe stop REAL por HTTP e revoga; após commit A prossegue e **não chama o adaptador simulado**.
8. Processos independentes comprovam serialização do guard de envio existente e do stop, com marcadores sintéticos sem rede.
9. Histórico com `PENDING`, `UNKNOWN` e `CONFIRMED` permanece íntegro; suíte geral e demais casos PostgreSQL herdados continuam passando.

## Registro de execução e pendências

- **CI do SHA final:** registrar links, SHA e contagens exatas após execução do GitHub Actions (não extrapolar números de um commit anterior).
- **PR:** manter `DRAFT` em branch base P0-05; não alterar `main`.
- **Riscos ainda abertos:** F02–F07 não abordados; flags REAL do Render não verificadas no workspace autorizado; clientes externos das APIs antigas ainda não inventariados; execução distribuída não comprova encerramento instantâneo dos workers; bloqueios DB durante requisições já iniciadas permanecem sujeitos a F04.

**Decisão desta etapa:** implementação e testes da F01 sujeita à revisão independente. Não é autorização para merge, deploy, execução REAL ou avanço para F02.

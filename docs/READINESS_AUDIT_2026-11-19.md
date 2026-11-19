# NEXORA IA — auditoria de prontidão técnica (19/11/2026)

**Escopo:** autenticação, privacidade, CI, confiabilidade, observabilidade e preparação para testes em ambiente de demonstração.  
**Fora do escopo:** habilitar ordens financeiras reais, remover limites de risco, aprovar oferta pública de serviços financeiros ou executar contratos para validar a implantação.

## Resumo executivo

**Estado: EM HOMOLOGAÇÃO — não aprovado para liberar a conta REAL.**

Esta revisão não deve ser interpretada como auditoria regulatória, autorização de comercialização ou certificação de segurança. Foram feitas consultas somente leitura ao GitHub e Supabase. Nenhuma operação financeira foi executada e nenhuma configuração de execução do Render foi alterada.

## Evidências confirmadas nesta revisão

| Item | Evidência | Resultado |
| --- | --- | --- |
| Código em produção pretendido | `main`, commit `d51aedba27458057e2803346da85f887fd7aede9`, 18/11/2026 | Verificado no GitHub; deploy Render não aferido |
| PR de segurança #31 | Pull request incorporada à `main` em 17/11/2026 | Confirmado no GitHub |
| GitHub Actions (último HEAD consultado) | `NEXORA Python tests`, `NEXORA repository security audit`, `NEXORA production public smoke check`, `NEXORA access and referral tests` | Execuções concluídas com `success` em 18/11/2026; não se afirma número de testes nesse HEAD |
| RLS `public` | `pg_tables`: 28 de 28 tabelas com `rowsecurity` | Confirmado via SQL de leitura em 19/11/2026 |
| Permissões `anon` e `authenticated` | `has_table_privilege` para SELECT e INSERT nas 28 tabelas | 0 tabelas com tais permissões diretas |
| Supabase Security Advisor | `rls_enabled_no_policy` (INFO) | Alerta informativo, consistente com backend SQLAlchemy sem cliente público |
| Supabase Performance Advisor | `unused_index` (INFO) em 9 índices | Não remover índices sem medir consultas e impacto |
| Smoke público existente | `/healthz` e `/api/session` | CI passou no HEAD consultado |

SQL executado (agregação sem ler informações dos usuários):

```sql
SELECT
  count(*) AS table_count,
  count(*) FILTER (WHERE rowsecurity) AS rls_enabled,
  count(*) FILTER (
    WHERE has_table_privilege('anon', format('%I.%I', schemaname, tablename), 'SELECT')
  ) AS anon_select,
  count(*) FILTER (
    WHERE has_table_privilege('authenticated', format('%I.%I', schemaname, tablename), 'SELECT')
  ) AS authenticated_select,
  count(*) FILTER (
    WHERE has_table_privilege('anon', format('%I.%I', schemaname, tablename), 'INSERT')
  ) AS anon_insert,
  count(*) FILTER (
    WHERE has_table_privilege('authenticated', format('%I.%I', schemaname, tablename), 'INSERT')
  ) AS authenticated_insert
FROM pg_tables
WHERE schemaname = 'public';
```

Resultado: `table_count=28`, `rls_enabled=28`, `anon_select=0`, `authenticated_select=0`, `anon_insert=0`, `authenticated_insert=0`.

**Limite da prova:** as contagens não substituem teste de isolamento entre sessões de usuários, privilégios por outras roles, exposição por futuras tabelas, inspeção de segredos ou validação da configuração efetiva do serviço.

## Reforço de segurança proposto nesta branch

A verificação CI `.github/workflows/render-public-smoke.yml` passa a consultar, **sem cookies ou tokens**, quatro endpoints privados com método GET:

- `/api/status`
- `/api/automation/profile`
- `/api/demo/orders`
- `/api/real/orders`

Critério: todos devem responder **HTTP 401**, com `Cache-Control: no-store`. A verificação falha se houver acesso anônimo ou resposta inesperada. Ela **não** chama endpoints de ativação, envio de ordens, login ou mudança de configuração.

**Importante:** a alteração é uma proposta em PR. A execução do CI após o PR é necessária para confirmar que o novo teste passa; os testes anteriores não cobrem automaticamente a modificação recém-criada.

## Testes de isolamento acrescentados à PR

Arquivo: `tests/test_readonly_tenant_isolation.py`.

- Dois usuários e duas sessões sintéticas são criados em SQLite temporário.
- A API de perfil e os históricos de contratos Demo/Real devem devolver apenas dados do usuário autenticado, inclusive quando é fornecido um parâmetro `user_id` estrangeiro.
- Quatro endpoints privados devem negar acessos sem cookie.
- Uma sessão revogada deve deixar de acessar os dados imediatamente.
- Os testes usam apenas registros fictícios e métodos GET; **não enviam ordens nem estabelecem conexão com a corretora**.
- O resultado dos testes depende da execução CI no commit atualizado; não assumir aprovação com base nas execuções da PR anterior.

## Diagnóstico preliminar do banco (somente leitura)

Consulta agregada ao Supabase em 19/11/2026, sem examinar dados de usuários:

| Métrica | Valor observado |
| --- | --- |
| Conexões PostgreSQL no instante da consulta | 13 (11 idle, 1 active, 1 unknown) |
| Deadlocks reportados por `pg_stat_database` | 0 |
| `demo_orders`: linhas vivas estimadas | 1.096 |
| `demo_orders`: varreduras sequenciais acumuladas | 354.648 |
| `demo_orders`: buscas por índice acumuladas | 150.027 |
| `demo_orders`: tamanho total com índices | 488 kB |

**Interpretação:** os contadores de `pg_stat_*` são acumulados desde o último reset das estatísticas e não representam a taxa atual nem o tempo de uma requisição. O número de varreduras sequenciais merece investigação, mas não justifica apagar índices ou aplicar migração sem `EXPLAIN` e medição de latência. O volume de dados é relativamente pequeno, logo nenhum desses contadores, isoladamente, identifica a causa da lentidão da interface.

**Limitação Render:** métricas privadas do serviço ainda não foram consultadas; a conexão requer seleção explícita do workspace pelo proprietário.

## Pendências para homologação

### Prioridade 1 — segurança / controle de acesso
- [ ] Validar login e sessão após migração de RLS, inclusive encerramento, expiração e acesso sem autenticação.
- [ ] Executar testes de isolamento entre duas contas distintas; uma não pode ler ou alterar dados de outra.
- [ ] Revisar proteção de `main` no plano GitHub disponível; alterações devem passar por PR e CI antes de qualquer merge.
- [ ] Conferir no painel Supabase as opções de exposição automática de tabelas futuras e os privilégios padrão (sem conceder acesso público).
- [ ] Revisar a gestão de credenciais e o histórico do GitHub quanto a possíveis segredos expostos, sem copiá-los para relatórios.

### Prioridade 2 — confiabilidade / desempenho
- [ ] Medir tempos de primeira abertura (Render Free), API de sessão, status e painel; coletar p50/p95 de latência.
- [ ] Verificar logs do Render depois da PR #31: reinícios, `ConnectionClosedError`, CPU, memória e conexões Postgres.
- [ ] Fazer teste de carga somente leitura/simulado com múltiplas sessões, sem enviar contratos.
- [ ] Validar idempotência, reconciliação e interrupção segura diante de falhas usando mocks e dados sintéticos.
- [ ] Validar rotinas de backup e **restauração** com dados fictícios; monitoramento de falhas e canal de suporte.

### Prioridade 3 — produto / conformidade
- [ ] Confirmar com análise jurídica e termos dos provedores se a atividade pretendida é permitida nos mercados atendidos, inclusive elegibilidade por idade.
- [ ] Revisar avisos de risco, publicidade, suporte, tratamento de dados e resolução de incidentes.
- [ ] Distinguir no produto resultados simulados de resultados confirmados, sem prometer taxa de acerto ou lucratividade.
- [ ] Obter avaliação independente do risco e das obrigações aplicáveis antes de qualquer decisão de oferta com fundos reais.

## Infraestrutura e limitações desta auditoria

- **GitHub:** branch `main` e CI consultados; este documento está em uma branch de revisão, não em produção.
- **Supabase:** consultas agregadas e advisors verificados, sem migrações ou modificações.
- **Render:** valores de variáveis de ambiente, métricas privadas, logs recentes e deploy efetivo **não foram verificados** nesta revisão. O workflow de monitoramento não comprova disponibilidade contínua nem estado do robô.
- **Corretora:** nenhuma conta autenticada de usuário nem contrato real foi consultado ou acionado.

## Render — verificação do serviço no workspace confirmado (19/11/2026)

Workspace `My Workspace` (`tea-dbqd7rqjnfac738bd5dg`), serviço `Nexoria-Ia` (`srv-dbqd9rdg1s2s73e0difg`). **Apenas leitura**, sem alterar variáveis, deploy, escala ou contas.

| Campo | Resultado |
| --- | --- |
| Versão efetivamente implantada | Deploy `dep-dbv0orbm8hqs73afnc90`, status `live`, commit `d51aedba27458057e2803346da85f887fd7aede9` |
| Data do deploy | 18/11/2026 às 20:38 UTC |
| Plano / quantidade configurada | Render `free`, 1 instância; Uvicorn com 1 worker |
| Região | Oregon |
| Health-check HTTP configurado | `healthCheckPath` vazio; investigar adoção de `/healthz` após validar que o endpoint reflete prontidão sem depender de serviços externos transitórios |
| Limites de recurso observados | CPU 0,15 unidade; memória cerca de 537 MB (métricas do Render) |
| CPU observada | ~0,013–0,023 unidade por ponto; sem evidência de saturação constante nessa janela |
| Memória observada | ~157–177 MB por ponto; abaixo do limite exibido |
| HTTP p50, p95 e contagem de requisições | Render retornou séries vazias nesse serviço; **não é possível calcular tempo de resposta ou taxa de erro a partir delas** |

Janela de amostragem: aproximadamente 18/11 às 18:00 UTC até 19/11 às 18:45 UTC, resolução de 15 minutos nas métricas de recurso. São amostras, não máximos absolutos entre coletas. Recursos podem se comportar de forma diferente sob tráfego real.

### Interrupções de consulta pública à Deriv

Os logs do Render contêm avisos `ConnectionClosedError` de consultas públicas, principalmente `endpoint=candles`. Os intervalos abaixo foram consultados separadamente e os resultados não foram paginados porque cada consulta indicou `hasMore=false`:

| Intervalo UTC | Avisos retornados |
| --- | ---: |
| 18/11 18:00–20:38 (antes do último deploy) | 20 |
| 18/11 20:38–19/11 04:00 | 51 |
| 19/11 04:00–11:00 | 40 |
| 19/11 11:00–18:45 | 6 |

O código `app/market.py` mantém até duas conexões WebSocket públicas reutilizáveis e tenta novamente após interrupção. Muitos desses avisos registram `attempt=1/2`, não uma falha definitiva após retry. **Não se pode inferir taxa de falha de ordens, desconexão de todos os usuários ou queda real de serviço apenas pela quantidade de avisos.** A redução na janela final é observável, mas requer acompanhamento em períodos comparáveis antes de concluir que o problema foi resolvido.

Consultas filtradas por HTTP 5xx não retornaram registros no intervalo examinado, mas, sem contagem total de solicitações e diante da ausência de métricas HTTP, isso **não comprova taxa zero de erros**.

### Instrumentação mínima proposta (ainda em PR)

O workflow de smoke passa a registrar `elapsed_ms` em cada requisição pública já existente (`/healthz`, `/api/session` e 4 GETs de acesso negado). Isso é latência observada a partir do runner CI, com pouquíssimas amostras e possíveis efeitos de rede/cold start; **não é um p95 real dos usuários**. A alteração não aumenta o número de chamadas e não executa ordens. Precisa passar no CI da revisão atualizada.

### Próximas ações

- [ ] Verificar logs de novos `ConnectionClosedError` em intervalos comparáveis e registrar taxa de falha definitiva após as tentativas, sem alterar a execução de ordens.
- [ ] Obter métricas reais de latência p50/p95 e volume HTTP por rota via instrumentação observacional segura; a API Render devolveu dados vazios.
- [ ] Avaliar a configuração de health-check HTTP `/healthz` em homologação, sem aplicá-la antes de testar o endpoint e o comportamento de reinício.
- [ ] Rodar teste de carga **somente de leitura e com dados sintéticos**, acompanhado de CPU, memória e tempos de resposta.
- [ ] Confirmar backups, restauração e plano de monitoramento antes de concluir homologação.

## Otimização conservadora de interface — 19/11/2026

A auditoria do `app/index.html` identificou solicitações repetidas por telas:

- Ao entrar no painel por `go('dashboard')`, a interface executava `refreshDashboard()` e `loadDemoOrders()` imediatamente. Em caso de sucesso, `refreshDashboard()` já chama `loadDemoOrders()`, criando duplicidade.
- No fluxo de sessão autenticada de `init()`, a mesma duplicidade ocorria.
- A página de Resultados possui um polling específico de histórico a cada **3 segundos**; adicionalmente, um timer genérico chamava `refreshDashboard()` a cada **30 segundos** mesmo quando o painel não estava visível. Isso criava chamadas extras a vários endpoints de saldo, acesso e status.

**Alteração proposta na PR (não publicada):**

1. Remover a segunda consulta de histórico nos dois caminhos de entrada **quando `refreshDashboard()` concluir normalmente**. Caso a atualização retorne `null`, manter o GET de histórico como fallback de exibição, sem comandos POST.
2. Restringir o timer genérico de 30 segundos à página Dashboard, deixando a página Resultados sob seu polling dedicado de histórico.
3. Preservar intervalos de 7s no Dashboard, 3s em Resultados, a atualização dos indicadores de resultado diário, a reconciliação oficial e os controles de ativação.
4. Adicionar testes de regressão estáticos de interface em `tests/test_demo_ui_flow.py`.

Impacto esperado: menos leituras redundantes e menos atualizações fora da página visível. **Não há ainda benchmark p50/p95 antes/depois**, portanto não afirmar uma melhora percentual, nem confundir redução de chamadas GET com melhor qualidade ou velocidade de execução de operações.

## Ensaio de concorrência local e recuperação de tela — 19/11/2026

**Ambiente:** GitHub Actions Ubuntu + Python 3.12 + SQLite temporário. Não é benchmark no Render nem na Deriv; não acessa dados de clientes, não chama APIs do broker e não dispara ordens.

- `tests/test_readonly_dashboard_load.py`: 10 usuários sintéticos independentes, 3 passes por usuário em 3 endpoints GET privados (`/api/automation/profile`, `/api/demo/orders`, `/api/real/orders`) = **90 leituras**, com `ThreadPoolExecutor(max_workers=10)`. Exige isolamento dos históricos em cada resposta.
- O teste evita iniciar o lifespan de `TestClient`, para que scanners e tarefas de fundo não sejam ativados. As funções de compra também são substituídas por mocks que rejeitam chamadas.
- `.github/workflows/test.yml`: testes gerais e ensaio de concorrência executados em etapas separadas para publicar `p50`/`p95` nos logs do CI. Nenhuma meta numérica de tempo é imposta, pois a velocidade varia entre runners.
- `tests/test_dashboard_failure_recovery.py`: executa com Node o código real de `refreshDashboard()` extraído do HTML e simula primeiro um GET de status com falha, depois uma resposta válida. Verifica que a falha não troca o estado visível do robô por um falso desligamento e que a recuperação volta a usar o estado do servidor, sem POSTs.

| Execução CI | Leituras | p50 local | p95 local | Máximo local |
| --- | ---: | ---: | ---: | ---: |
| Commit `60fcd0f5` (19/11) | 90 | 70,41 ms | 105,34 ms | 119,98 ms |
| Commit `45c1128e` (19/11) | 90 | 48,82 ms | 79,29 ms | 87,24 ms |

Na última execução consultada (`45c1128e`), **311 testes gerais passaram**, o ensaio separado de 10 usuários passou e os outros três fluxos CI (segurança, progressão e smoke público) passaram. Os tempos variam entre execuções mesmo sem alteração na lógica medida.

**Limites dos resultados:** são leituras locais em SQLite sob concorrência de threads e não medem latência entre cliente, Render (Oregon), Supabase e corretora, nem carga de 10 sessões reais. A meta de testar 10 usuários simultâneos em Demo hospedado, com p95 e recursos Render, **permanece pendente**.

## Ajuste de carga em segundo plano — 19/11/2026

Em `app/index.html`, timers de leitura continuavam atualizando o dashboard enquanto a aba estava invisível. A PR passa a ignorar execuções dos timers **somente de interface** quando `document.hidden` é `true`:

- Histórico e monitor de leitura (3 segundos na página Resultados ou 7 segundos no Dashboard).
- Atualização geral do Dashboard (30 segundos).
- Exibição dos resultados diários (120 segundos).

Os timers existentes continuam programados e voltam a consultar dados quando a página fica visível; não é criada uma segunda sequência de timers. As chamadas disparadas por ações explícitas da pessoa continuam intactas. **Essa verificação não pausa, inicia nem altera o robô ou seus controles financeiros do servidor**.

O teste `tests/test_dashboard_visibility_polling.py` executa a implementação de `updateResultsPolling()` em Node com mocks dos timers e valida as duas situações: sem GET na aba oculta e retomada automática em aba visível. Um teste estático garante que o timer genérico e a leitura de resultados diários também ignoram abas ocultas.

**Benefício esperado:** menos solicitações GET redundantes em abas em segundo plano, sem prometer melhoria quantitativa antes de medições de produção. Teste de CI desta mudança deve ser concluído antes do merge.

## Critério de conclusão

Só classificar este trabalho como *homologação técnica concluída* após registrar evidências reproduzíveis para controle de acesso, CI, desempenho, falhas de rede/reinício, isolamento entre usuários, restauração, monitoramento e revisão de conformidade. Homologação técnica **não** equivale a autorização para ativar negociação automática com dinheiro real.

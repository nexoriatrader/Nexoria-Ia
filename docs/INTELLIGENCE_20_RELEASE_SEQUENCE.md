# NEXORA 2.0 — sequência de integração progressiva (release candidate)

**Status:** release candidate SOMENTE LEITURA; não autorizada para deploy
automático nem para merge. Nesta etapa só há preparação verificável de
liberação, sem alteração da operação financeira. Base de código:
I04 PR #54 SHA \`0d2aac586d9294068cf8c1501712367947ced603\`.
Branch de release: \`release/intelligence20-i01-i04-readonly-rc\`.
Produção \`main\` vista na revisão inicial: \`d51aedba27458057e2803346da85f887fd7aede9\`.

## Bloqueio técnico de merge "simples"

GitHub compare de \`main\` para I04: 231 commits à frente, 68 arquivos
modificados, incluindo P0-01..05, F01, I01..I04, mudanças no esquema do
PostgreSQL e código do executor REAL existente. As PRs #45–#54 são
DRAFT e stacked. Fazer merge de I04/release diretamente em \`main\`
não isola a inteligência: também instala P0/F01 e poderá exigir
\`f01_global_fencing_v1\` no startup. Até que o esquema PostgreSQL
de produção e as transições de segurança sejam revisados e autorizados,
**não realizar merges/deploys que incluam esse conjunto**.
A autorização para migração I04 NÃO está implícita neste trabalho.

## Conjuntos já integrados no código da release candidate

| Camada | Integração comprovável | Liberação |
|---|---|---|
| I01 | \`intelligence2.market_data.read_closed_window\` reutiliza o cliente público e dedup/candles fechados existentes | Apenas consulta |
| I02 | \`asset_scanner.scan_intelligence_markets\`, opt-in \`/api/research/asset-scanner?version=2\` | Pesquisa autenticada |
| I03 | \`asset_scanner.scan_intelligence_context\`, opt-in \`version=3\`, contexto compartilhado por TF | Pesquisa autenticada |
| I04 | Knowledge Base nos modelos do banco existente, rotas \`/api/research/knowledge\`, cache zero | **OFF** até migração e habilitação explícita |
| F01 | Startup PostgreSQL sem DDL, migração formal exigida | Preservar fencing e consentimento REAL |

O compartilhamento de inteligência é exclusivamente de dados públicos.
Nenhum código I01–I04 concede execução de operações.

## Etapas de implantação com condições de entrada/saída

### R0 — Candidata integrada, sem deploy
- Conferir sequência de ancestry PR45..PR54; reexecutar CI completo no
  SHA candidato, incluindo teste de read-only por usuário, P0/F01,
  PostgreSQL16 descartável, ausência de DDL no startup e permissões.
- Aprovar separadamente a incorporação das alterações P0/F01 e revisão
  do impacto da migração F01 exigida ao inicializar no PostgreSQL.
- Congelar SHAs \`main\`, F01, I01, I02, I03, I04 e plano de rollback.
- Se não houver confirmação para uma dependência, liberar somente
  subconjuntos comprovadamente isolados; não "forçar" merge stacked.

### R1 — Publicação de I01–I03 somente leitura
- Somente após os gates da etapa R0, conferir no Render a branch de
  deploy, auto-deploy, DB configurado, variáveis e histórico de release.
- Flags financeiras continuam **false**. Não usar contas financeiras
  na verificação. Não criar segundo feed/worker/serviço.
- Deploy controlado sem usar \`/api/market/quote\`, compras ou ordens.
- Pós-deploy: \`GET /healthz\`=200; \`GET /api/session\`
  anônimo=200 com authenticated false; rotas scanner V1/V2/V3
  sem sessão=401; com sessão sintética de staging,
  conferir cache versionado e ausência de dados de contas no corpo
  público. Conferir latência e erros sem expor credenciais.
- Monitorar logs, uptime e consumo do feed já existente. Se erro ou
  regressão: desativar a versão exposta / restaurar release anterior
  de aplicação sem tocar no F01 ou no banco.

### R2 — Migração I04, **depende de aprovação específica**
1. **Antes**: inventário de schema real usando somente SELECT em
   \`pg_catalog\`; conferir F01 revision e singleton, colunas, constraints,
   chaves e conectividade; compare schema com SQL I04 formal. Capturar
   esquema sem exportar dados pessoais para logs.
2. **Backup independente**: snapshot/restaurabilidade via provedor do
   PostgreSQL e \`pg_dump --format=custom\` da instância autorizada,
   guardado em destino seguro fora do runtime, com checksum e controle
   de acesso. Validar o restore em uma **instância isolada**, com
   acesso administrativo e nenhuma conexão da aplicação. Não realizar
   \`pg_dump\` ou cópia dos dados da produção nesta etapa sem autorização.
3. **Ensaio**: restaurar snapshot em PostgreSQL 16 descartável isolado.
   Executar \`migrations/I04_KNOWLEDGE_BASE_V1_POSTGRES.sql\`
   com transaction, timeout e checagem estrita de estrutura.
   Testar idempotência, rollback de falha, \`init_db()\` zero DDL,
   busca inativa, registros públicos sem PII, F01 intacto.
4. **Janela autorizada**: preparar pontos de corte, responsável pela
   aprovação, lock_timeout e caminho de rollback. Aplicar I04 formal
   somente se backup/ensaio e revisão independente forem aprovados.
   Verificar revision \`i04_knowledge_base_v1\`, F01, objetos novos e
   ausência de alteração em ledger financeiro.
5. **Habilitação gradual**: primeiro manter
   \`NEXORA_INTELLIGENCE_KNOWLEDGE_ENABLED=false\` durante deploy
   da aplicação; habilitar em janela separada após validação.
   Ler apenas endpoints KB autenticados. Nenhuma fonte ML é ativada.
6. **Rollback lógico preferencial**: voltar a flag I04 para false,
   retornar release anterior da aplicação conforme compatibilidade
   observada, PRESERVANDO as tabelas novas e decisões auditáveis;
   NÃO executar DROP nem restaurar snapshot de produção em operação
   para "desfazer" uma migração aditiva. Se surgir corrupção estrutural
   severa, tratar como incidente: manutenção, autorização separada,
   isolamento, restauração validada com análise de perda de dados.
7. Pós-janela: evidência de consultas, execução do \`init_db()\` sem
   DDL, contagem mínima dos objetos e status das PRs/deploys.

### R3 — I05 Learning Engine
- Somente pesquisa de regimes públicos enquanto testes/champion-challenger
  forem validados fora da amostra. Ativação e promoção de conhecimento
  modelo requerem processo próprio; nenhum ajuste de estratégia nem
  direção de operação é permitido por esta release. A I05 segue PR
  separada, sem depender de deploy antecipado.
- Modelo Fallback sempre é o comportamento de pesquisa conhecido (ou
  \`NO_VALIDATED_MODEL\`); degradação de pesquisa nunca envia ordens.

## Responsabilidades e decisões não tomadas

- Não escolher automaticamente o workspace Render: requer confirmação
  de proprietário, serviço/branch e estado auto-deploy.
- Nenhuma execução de DDL ou backup de produção, nem deploy/merge nesta
  release candidate.
- Nenhuma autorização para trading REAL ou mudança financeira. Todas as
  flags REAL permanecem false.
- Não equiparar CI verde à autorização operacional de migração.

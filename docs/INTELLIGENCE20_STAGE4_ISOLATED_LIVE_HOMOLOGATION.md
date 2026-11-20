# NEXORA Intelligence 2.0 — Etapa 4, homologação isolada

Este é um incremento de software sobre a PR #65, conforme o PDF de Etapa 4 fornecido. NÃO é evidência de feed real nem de deploy. O CI anterior usa feed sintético e não comprova a Etapa 4.

## Entregue em código
- Serviço ASGI separado: app.intelligence2.stage4_homologation_service:app.
- Não inicializa app.main nem as rotinas financeiras; possui somente healthz, painel, status e leitura autenticada de pesquisa.
- Reutiliza o worker, a fila com fencing, os módulos I01–I06/I04/I05 e a fonte pública/catálogo da Deriv existentes, sem cliente ou scanner duplicados.
- Loop opt-in de 60 a 300 segundos, padrão 90; tarefa idempotente por candle M1 fechado.
- GET /api/homologation/robot/research?view=autonomous|signals|advanced com header X-NEXORA-HOMOLOGATION-KEY.
- GET /api/homologation/status retorna status persistido e último ciclo em memória; não afirma que o feed é real.
- GET / mostra painel somente de leitura com chave fornecida pelo operador, mantida em memória no navegador.
- Script explícito offline de migração para PostgreSQL novo e vazio, executa F01 -> I04 -> coordenação após criar tabelas base vazias.

## Configuração MANUAL em Render, sem tocar em produção

1. Selecione explicitamente o workspace Render correto. Crie novo PostgreSQL e novo web service isolados. NÃO use o serviço existente nexora-ia nem banco de produção.
2. Web service branch: feature/intelligence20-stage4-isolated-live-homologation, Python 3.12, build: pip install -r requirements.txt, start: uvicorn app.intelligence2.stage4_homologation_service:app --host 0.0.0.0 --port $PORT --workers 1. Desative auto-deploy para esta validação.
3. Configure DATABASE_URL do NOVO banco. Defina as variáveis abaixo, exclusivamente nele. Nunca copie chaves, sessions ou contas de produção.
4. Antes do primeiro start, em executor de manutenção com acesso ao banco novo, execute uma vez: python scripts/bootstrap_intelligence20_homologation.py --confirm-fresh-isolated-homologation. O script recusa banco que já contenha tabelas. O serviço web não cria tabelas.
5. Valide login com chave de homologação, status 401 sem chave / 200 com chave e disponibilidade de snapshots. Sem segredos em URLs ou logs.

Variáveis obrigatórias:
- NEXORA_DEPLOYMENT_STAGE=homologation
- DATABASE_URL=URL_EXCLUSIVA_DO_NOVO_POSTGRES
- NEXORA_HOMOLOGATION_DATABASE_NAME=nome_exato_contendo_homolog
- NEXORA_HOMOLOGATION_DATABASE_HOST=host_exato_do_novo_postgres
- NEXORA_HOMOLOGATION_ACCESS_TOKEN=segredo_unico_aleatorio_de_32_a_256_caracteres
- NEXORA_INTELLIGENCE_HOMOLOGATION_READONLY_ENABLED=true
- NEXORA_INTELLIGENCE_DISTRIBUTED_RESEARCH_ENABLED=true
- NEXORA_INTELLIGENCE_HOMOLOGATION_LIVE_WORKER_ENABLED=true
- NEXORA_REAL_AUTOPILOT_ENABLED=false
- NEXORA_REAL_RELEASE_APPROVED=false
- NEXORA_DEMO_BUY_ENABLED=false
- NEXORA_HOMOLOGATION_CYCLE_INTERVAL_SECONDS=90

## Gates de homologação ainda PENDENTES

- URL real Render e service ID; logs de build/inicialização; banco PostgreSQL separado com as três revisões.
- Comprovação independente de recebimento de OHLC fechado da Deriv real, timestamps/validade e comportamento sob limites de API; sucesso de um ciclo por si só não comprova isso.
- Leituras atualizadas por todas as visualizações e persistência após reinicialização somente do serviço isolado.
- Testes de recuperação de conexão e observação contínua por 60–120 min, sem dados fabricados.
- Registrar logs saneados, CI do SHA final e resultados em PR DRAFT; não declarar 500 usuários validados.

NENHUM merge, deploy, ativação REAL ou migração produtiva faz parte desta entrega de código. Para criar recursos Render é obrigatório o usuário escolher explicitamente o workspace. 

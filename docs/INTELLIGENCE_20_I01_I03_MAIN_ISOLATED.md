# NEXORA Intelligence 2.0 — Primeira candidata independente I01–I03

**Objetivo:** liberar somente pesquisa pública autenticada, sem transportar a
cadeia de 231 commits P0/F01/I04 da PR #55. A base é a `main` de produção:
`d51aedba27458057e2803346da85f887fd7aede9`.

## Código entregue

- I01: `app/intelligence2/market_data.py` usa o leitor existente
  `market.shared_candle_request`, valida candles completos/fechados e não
  introduz WebSocket, executor nem cache paralelo.
- I02: `market_scanner.py` e integração por `scan_intelligence_markets`
  reutilizam catálogo público `automation_markets` e
  `asset_scanner.py`, com concorrência limitada, resultados por mercado
  e exclusões explicadas.
- I03: `context_engine.py` e `timeframe_engine.py` classificam
  contextos e hierarquias de timeframes para pesquisa, falhando com
  segurança quando há lacunas, falta de dados ou conflito.
- Única API já existente: `GET /api/research/asset-scanner`.
  Versão 1 preservada; versão 2 pesquisa I02; versão 3 pesquisa I03,
  com `expiry_minutes` limitado a 1, 3, 5 ou 15.
- Dados públicos são cacheados no mesmo processo, de forma limitada
  (cinco variantes e TTL de 60 segundos). Consulta dos contratos
  próprios do usuário é feita **depois** da leitura do cache público,
  sem incluir dados financeiros do usuário no cache compartilhado.

## Liberação progressiva — não executada

A única nova flag para disponibilização das versões 2/3 é:

```text
NEXORA_INTELLIGENCE_READONLY_ENABLED=false
```

O padrão seguro é OFF: versões 2/3 devolvem HTTP 503. As rotas exigem
sessão; sem autenticação devolvem 401. Versão 1 segue funcionando como
antes. Nenhuma interface de compra, rotina financeira, tabela ou
migração foi alterada. Não define nem modifica flags REAL e não
ativa I04/I05. Não há alteração de esquema exigida por esta candidata.

### Critérios objetivos da primeira implantação (dependem de aprovação)

1. Conferir CI final deste SHA, inclusive os testes de regressão e a
   execução descartável PostgreSQL 17.11.
2. Confirmar configuração efetiva `DATABASE_URL` e flags financeiras
   **sem ler/divulgar segredos**. A ferramenta Render disponível
   ainda não forneceu confirmação da conexão ao Supabase consultado.
3. Registrar rollback para o deploy atualmente LIVE da `main`
   (`d51aedba27458057e2803346da85f887fd7aede9`).
   Observar o auto-deploy ativo: merge em `main` pode iniciar deploy.
4. Solicitar aprovação separada para merge/deploy da candidata, com
   pesquisa ainda OFF. Após verificar health e autenticação, solicitar
   aprovação separada para habilitar a flag I01–I03, começando por
   consultas autenticadas com testes de acesso, cache e integridade.
5. Em erro: manter a flag OFF; se a versão 1 ou saúde do serviço
   regredir, restaurar o artefato anterior da aplicação. Não existe
   migração I01–I03 para reverter.

**Proteções preservadas:** todos os arquivos `app/db.py`,
`app/real_*.py`, `app/demo_*.py`, ledger e migrações continuam
exatamente como estão na `main`. A candidata NÃO incorpora P0/F01
pendentes; portanto não os enfraquece nem finge que estão ativos.

## Continuidade

I04 depende de migração aditiva formal e aprovação própria; I05
permanece em branch separada, com treinamento e promoção OFF. Nenhuma
delas é um pré-requisito para a API pública I01–I03.

Este documento não autoriza merge, deploy, backup produtivo, mudança
de variáveis ou ordens Deriv. Os testes são evidência de código em
ambiente descartável, **não** de capacidade validada para centenas
de usuários simultâneos.

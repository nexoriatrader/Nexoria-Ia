# NEXORA IA — Identidade de contas, OAuth e consultas sem negociação

**Data da revisão:** 19/11/2026  
**Status:** alteração de backend apenas de leitura, sob revisão na PR #44.  
**Não é uma aprovação de operações financeiras reais.**

## Diagnóstico factual da arquitetura existente

- **OAuth e sessão:** `app/security.py` constrói o fluxo OAuth PKCE com `state` e verifier. `app/main.py` armazena o token OAuth cifrado em sessão, vinculando o usuário ao identificador Demo confirmado no cadastro. `active_session` verifica o cookie, expiração e usuário antes de decifrar o token para uma requisição autenticada.
- **Conta Demo:** `app/demo.py` utiliza o identificador Demo do usuário para consultar saldo/contratos, em canal Demo separado; `/api/demo/balance` rejeita `selected_mode` diferente de Demo.
- **Descoberta de contas:** `/api/accounts/available` solicita a lista vinculada ao token OAuth e somente classifica uma conta como Real se a listagem a identificar explicitamente como tal, via `app/auth_helpers.py`.
- **Seleção:** `POST /api/automation/profile` verifica se o identificador Real escolhido consta na conta OAuth atual, e o armazena em `automation_profiles.real_account_id`.
- **Consultas Real:** `app/real_readonly.py` faz somente consultas de saldo e resultado, em canal WebSocket Real obtido por OTP de conta. Esse módulo não envia compra, proposta ou venda.
- **Ordens e riscos:** código de execução financeira Real e ledger existem em módulos separados. Nenhum deles foi alterado nesta revisão.

## Problema encontrado

As rotas de leitura `GET /api/real/balance` e `GET /api/real/results/today` invocavam `real_balance(accounts)`, que retornava a **primeira conta Real da lista**, sem verificar a seleção atual do usuário. Assim, quando a Deriv retornava mais de uma conta Real, a interface podia apresentar dados da conta errada. A rota `GET /api/real/orders` também podia agregar históricos de várias contas da mesma pessoa.

## Correção implementada na PR #44

1. **`app/auth_helpers.py`:** `real_balance(accounts, account_id=None)` aceita o identificador explicitamente selecionado e só o aceita se for uma conta Real identificada na listagem. Com duas contas Real e sem seleção, não retorna dados arbitrários; com apenas uma conta, preserva a compatibilidade.
2. **`app/main.py`:** `readonly_real_account_selection` busca a configuração salva da sessão e confirma a propriedade da conta na lista OAuth atual. As rotas de saldo e resultado do dia usam a mesma conta. Se a seleção for ambígua ou antiga, a API retorna `available=false` com motivo, sem chamar qualquer WebSocket de saldo ou resultados de outra conta.
3. **`app/main.py`:** o histórico Real é filtrado pela conta salva. Sem seleção e com mais de uma conta histórica, retorna lista vazia e solicita escolha; com uma única conta histórica mantém o comportamento de leitura prévio. Nenhuma linha do ledger é apagada.
4. **`tests/test_readonly_real_account_selection.py`:** testes com usuário, token e contas fictícios verificam seleção, ausência de seleção, conta revogada, isolamento de histórico, token passado às consultas de leitura e manutenção da consulta Demo independente.

## Evidências e limites

Os testes foram construídos com SQLite temporário, respostas OAuth sintéticas e mocks das funções que chamariam a corretora. Não foram consultadas credenciais reais nem enviados contratos, compras ou propostas.

Esta correção não realiza nem autoriza a execução em conta Real; não modifica Martingale, ciclos, multiplicadores, saldos comprometidos, configuração de risco ou APIs de compra. O OAuth fornece um token de sessão que autoriza a solicitação de dados de contas; a conta do canal de leitura é identificada separadamente pelo `account_id` verificado e OTP correspondente.

A listagem da Deriv pode omitir um saldo. Nesse caso, o código existente consulta o saldo via WebSocket **somente leitura** da conta Real selecionada. A atualidade dos saldos exibidos não foi aferida com contas reais nesta revisão.

## Pendências não comprovadas

- Validação com contas reais de teste **sem envio de ordens** por um responsável autorizado, incluindo mudanças de conta e expiração da sessão.
- Mensuração da frequência das consultas de saldos e tempos de resposta em Render e Deriv.
- Restauração de backup, teste de carga hospedada, incidentes e conformidade jurídica/regulatória por especialistas competentes.
- Auditoria independente de qualquer mecanismo de ordens financeiras antes de consideração de distribuição pública.

**Critério:** nenhuma mudança nesta PR deve ser interpretada como autorização para liberar execução financeira real, Martingale ou ciclos financeiros com dinheiro real.

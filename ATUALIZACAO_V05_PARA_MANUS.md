> **ATUALIZAÇÃO DE PRIORIDADE (v0.5.1):** Leia `PROMPT_MANUS_DEMO_SEM_BLOQUEIOS_DESNECESSARIOS.md` antes de executar este documento. A conta Deriv DEMO pode e deve ser usada para compras supervisionadas com dinheiro virtual, habilitando `NEXORA_DEMO_BUY_ENABLED=true` no ambiente de teste após revisar controles. Isso NÃO autoriza dinheiro real.

# NEXORA IA — v0.5, correções auditáveis para Manus

## Leia antes de publicar

Este pacote é uma continuação do backup original enviado em 11/11/2026. **NÃO é uma versão comercial pronta**. Foram feitos testes locais, sem conta Deriv autenticada, sem executar compras e sem publicar alterações no GitHub ou na Render.

### Novidades implementadas

1. `app/demo_ledger.py`: ledger durável por conta com reserva de operação, chave de idempotência, um slot ativo por usuário no PostgreSQL, estados PENDING/CONFIRMED/UNKNOWN/SETTLED e liquidação exclusivamente a partir de lucro oficial retornado pela corretora.
2. `app/db.py`: nova tabela `demo_orders`, isolada por usuário. `Base.metadata.create_all()` cria a tabela faltante, mas **não substitui migrações Alembic** em futuras alterações estruturais.
3. `app/demo.py`: validação estrita de WebSocket `wss://api.derivws.com/trading/v1/options/ws/demo?...`, rejeição de preço de compra acima do valor autorizado.
4. `app/main.py`: `/api/demo/buy` exige `confirm=DEMO_ONLY`, `request_key` estável, modo demo, limite de exposição e flag `NEXORA_DEMO_BUY_ENABLED=true`. A flag é **false por padrão**; habilite apenas em ambiente de demonstração controlado após teste manual supervisionado.
5. `/api/demo/orders` retorna apenas registros do usuário autenticado. `/api/demo/orders/{order_id}/reconcile` consulta a Deriv e grava liquidação oficial. `/api/demo/contract/{contract_id}` exige prova de propriedade no banco.
6. `app/index.html`: painel para consultar ordens oficiais demo, distinto do histórico virtual.
7. `tests/test_demo_ledger.py` e `tests/test_demo_endpoints.py`: cenário de replay, bloqueio, perda máxima, lucro não-finito, URL maliciosa, modo Martingale indisponível, flag e autorização.

### Restrição importante

O Martingale permanece disponível no motor de simulação, **mas permanece BLOQUEADO para compras na conta demo oficial**, até receber validação específica. O Stop Win/Stop Loss do executor oficial mede o PnL **acumulado no ledger demo registrado** e não reinicia a cada dia ou sessão. Isto é conservador, mas ainda precisa de design de janelas de sessão e reconciliação de históricos anteriores antes de eventual publicação comercial.

### Segurança de compra

`PENDING` é gravado antes de chamar a Deriv. Se falhar a chamada ou a confirmação, fica `UNKNOWN` e **novas compras são bloqueadas**. Sem `contract_id` confirmado, a recuperação exige investigação manual na Deriv. Não apague linhas nem libere slots desconhecidos automaticamente.

A compra direta não está conectada ao botão LIGAR no navegador. O botão atual controla **apenas simulação virtual**. Não confundir os dois mundos.

### Como testar

```bash
python -m pip install -r requirements.txt
python -m pytest -q
```

Resultado local durante o desenvolvimento: **26 testes aprovados** (sem API externa). O login OAuth público, a compra demonstrativa, a reconciliação ponta a ponta e a estabilidade da Render/Supabase **não foram testados externamente**.

### Próximas tarefas para Manus (ordem obrigatória)

1. Fazer revisão de código/segurança da v0.5; garantir isolamento entre usuários e concorrência real no Supabase.
2. Confirmar a persistência e eventuais migrações da tabela `demo_orders` em banco de testes (não alterar produção sem backup).
3. Testar OAuth/PKCE no domínio HTTPS de testes, sem compartilhar token.
4. Executar **uma única** compra real da conta **DEMO** com consentimento específico do proprietário, depois habilitar manualmente a flag de demonstração. Verificar perda máxima e reconciliação até SETTLED.
5. Validar serialização e semântica reais de `is_sold`, `profit`, `contract_id` e confirmar formato da API Deriv vigente.
6. Testar concorrência de duas requisições idênticas e diferentes contra PostgreSQL real, queda depois do `buy`, restart, logout, reconexão e isolamento de contas.
7. Implementar automação DEMO somente depois do ledger comprovado; nunca automatizar conta real nesta fase.
8. Revalidar matematicamente estratégias com divisão temporal fora da amostra, spread, payoff real, custos e latência. Não oferecer alegações de lucros.
9. Antes de disponibilizar ao público brasileiro, obter análise regulatória e contratual de Deriv/afiliados.

### Variáveis de ambiente

- `NEXORA_DEMO_BUY_ENABLED=false` (novo: manter desabilitado por padrão)
- `DERIV_CLIENT_ID` identificador público já existente
- `DERIV_REDIRECT_URI` URL HTTPS exata cadastrada na Deriv
- `NEXORA_PUBLIC_URL` endereço público Render
- `DATABASE_URL` segredo fornecido separadamente na Render
- `NEXORA_FERNET_KEY` segredo gerado e armazenado só na Render

O pacote **não contém credenciais privadas**. Preserve a marca NEXORA IA e o painel simples.

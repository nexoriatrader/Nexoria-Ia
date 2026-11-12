> **ATUALIZAÇÃO DE PRIORIDADE (v0.5.1):** Leia `PROMPT_MANUS_DEMO_SEM_BLOQUEIOS_DESNECESSARIOS.md` antes de executar este documento. A conta Deriv DEMO pode e deve ser usada para compras supervisionadas com dinheiro virtual, habilitando `NEXORA_DEMO_BUY_ENABLED=true` no ambiente de teste após revisar controles. Isso NÃO autoriza dinheiro real.

# Prompt de continuidade — NEXORA IA

Você é o próximo agente responsável por continuar o desenvolvimento do projeto NEXORA IA. Este prompt acompanha um backup completo e autocontido. Leia primeiro `docs/BACKUP_STATUS_ATUAL.md`, `docs/RESTORE.md`, `DERIV_DEMO_INTEGRATION.md`, `INFRAESTRUTURA_MANUS.md`, `MANUS_INSTRUCOES.md` e `README.md`.

## Estado confirmado

O projeto está publicado em https://nexoria-ia.onrender.com/ e o código está no repositório privado https://github.com/nexoriatrader/Nexoria-Ia. O snapshot deste backup corresponde ao commit `653b57c`, atualmente na branch `main`. Os testes automatizados foram executados e passaram: `13 passed`.

A aplicação usa Python 3.12, FastAPI, Uvicorn, SQLAlchemy, PostgreSQL/Supabase, WebSockets Deriv, Fernet e Render. O callback público usado é `https://nexoria-ia.onrender.com/auth/deriv/callback`. A conta real deve continuar bloqueada nesta fase.

## O que já foi implementado

Existe OAuth 2.0 com PKCE, estado de login protegido, sessão por cookie HttpOnly, token Deriv criptografado com Fernet, identificação fechada de conta demo, configuração individual persistida, histórico virtual segregado, feed público de ticks, motor local tick-based, Stop Win/Stop Loss do motor virtual e interface premium responsiva.

O módulo `app/demo.py` implementa o fluxo autenticado demo: OTP, validação obrigatória do endpoint `/trading/v1/options/ws/demo`, consulta oficial de `proposal`, compra demo protegida pela confirmação literal `DEMO_ONLY` e consulta de `proposal_open_contract`. O backend não aceita canal real e não existe caminho de operação real.

A landing atual usa a nova logo transparente fornecida pelo responsável, texto de apresentação sobre inteligência artificial, instrução para conectar a conta Deriv e três ícones minimalistas próprios em SVG para leitura de mercado, motor inteligente e gestão de risco.

## Próximas etapas prioritárias

1. Testar ponta a ponta a compra demo real com uma conta demo autorizada, sem inventar resultado.
2. Conectar a confirmação visual obrigatória de compra demo à interface, se ainda não estiver exposta no painel.
3. Acompanhar cada contrato aberto até liquidação oficial por `proposal_open_contract`.
4. Persistir resultado oficial do contrato demo no histórico do banco, separando claramente resultado Deriv Demo de simulação virtual.
5. Aplicar Stop Win e Stop Loss com base em liquidação oficial, sem prometer proteção contra falhas de rede.
6. Implementar estados auditáveis de ordem: `PENDING`, `CONFIRMED`, `SETTLED` e `UNKNOWN`, com reconciliação e sem repetir automaticamente uma compra de status desconhecido.
7. Testar isolamento entre dois usuários, expiração de sessão, reinício do serviço, bloqueio de conta real e falhas de conexão.
8. Só depois revisar desempenho e inteligência; nunca declarar taxa de acerto ou rentabilidade sem backtest correto, dados fora da amostra e custos/payout reais.

## Regras que não podem ser quebradas

Não colocar segredos no GitHub, no ZIP ou no código. Não incluir `DATABASE_URL`, senha Supabase, `NEXORA_FERNET_KEY`, tokens OAuth, cookies ou dumps de banco. Não habilitar conta real. Não afirmar que uma funcionalidade está concluída sem teste verificável. Não tratar o motor virtual como negociação real. Não usar o link de afiliado para captação sem avaliação de autorização e compatibilidade regulatória. Não executar compra real sob nenhuma condição nesta fase.

## Variáveis de ambiente

Use o `.env.example` somente como modelo. Em produção configure os valores no Render/Supabase:

```text
DERIV_CLIENT_ID=34VU8L3LDiaENrDGVjqQP
DERIV_REDIRECT_URI=https://nexoria-ia.onrender.com/auth/deriv/callback
NEXORA_PUBLIC_URL=https://nexoria-ia.onrender.com
DATABASE_URL=<segredo do Supabase>
NEXORA_FERNET_KEY=<segredo Fernet>
NEXORA_SYMBOL=1HZ100V
```

## Como começar o trabalho

```bash
cd /caminho/da/pasta
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest -q
```

Antes de editar, confira `git status`, leia os documentos citados e valide o estado live em `/healthz` e na landing pública. Depois de cada alteração, rode os testes, faça `git diff --check`, publique somente quando autorizado pelo fluxo da tarefa e registre o commit no documento de continuidade.

## Fontes técnicas

Use as referências oficiais Deriv presentes em `DERIV_DEMO_INTEGRATION.md`: OAuth/PKCE, workflows, WebSocket Demo, proposal, buy e proposal_open_contract. A origem da logo está registrada no documento de backup principal, assim como a origem dos emblemas enviados pelo responsável.

## Entrega esperada do próximo agente

Entregar mudanças testadas no GitHub e Render, informar exatamente o que foi validado, manter a conta real bloqueada, atualizar a documentação quando o estado mudar e criar novo backup somente quando solicitado. Se uma operação depender de login, OTP, confirmação de compra demo ou evidência da conta Deriv, não invente o resultado: registre como pendente e explique o teste necessário.

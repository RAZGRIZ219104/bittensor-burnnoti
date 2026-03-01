module.exports = {
  apps: [{
    name: 'burn-monitor',
    script: './burn_monitor.py',
    interpreter: './venv/bin/python',
    cwd: '/root/bittensor-burnnoti',
    autorestart: true,
    watch: false,
    max_memory_restart: '500M',
    env: {
      NODE_ENV: 'production'
    }
  }]
};

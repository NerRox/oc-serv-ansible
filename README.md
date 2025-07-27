# Пререквизиты
1. Установить python и ansible:

```bash
sudo yum install python3 ansible
```

2. Создание DNS записи при помощи dyn-dns

# Certs path

```bash
server-cert = /etc/letsencrypt/live/{{ ocservDNSName }}/fullchain.pem
server-key = /etc/letsencrypt/live/{{ ocservDNSName }}/privkey.pem
```

# Extra vars
ocserv_dns_name

# Collections 
ansible.posix
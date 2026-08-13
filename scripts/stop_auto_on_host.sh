touch /home/ssm-user/quant-run/wave2-validate-h2/STOP
( crontab -l 2>/dev/null | grep -v basis_auto ) | crontab -
echo "STOPPED — cron removed and STOP file set"
tail -10 /home/ssm-user/quant-run/wave2-validate-h2/auto.log 2>/dev/null

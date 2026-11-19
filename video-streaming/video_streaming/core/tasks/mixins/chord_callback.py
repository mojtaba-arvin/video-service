from celery import Task


class ChordCallbackMixin(object):
    request: Task.request

    def __call__(self, *args, **kwargs):
        """
        args=[[None, dict(key1=value1), dict(key2=value2), None,...], arg1, arg2]
        kwargs=dict(key3=value3, key4=value4)
        ->
        args=[arg1 ,arg2]
        kwargs=dict(key1=value1, key2=value2, key3=value3, key4=value4)
        """
        if args and len(args):
            args = list(args)
            for result in args.pop(0):
                if isinstance(result, dict):
                    kwargs.update(result)
            self.request.args = args
            self.request.kwargs = kwargs
            args = tuple(args)
        return super().__call__(*args, **kwargs)

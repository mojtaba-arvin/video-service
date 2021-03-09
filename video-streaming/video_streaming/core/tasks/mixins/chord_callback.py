from celery import Task


class ChordCallbackMixin(object):
    request: Task.request

    def __call__(self, *args, **kwargs):
        print(self.request.args)
        """
        args=[[None, dict(key1=value1), dict(key2=value2), None,...], arg1, arg2]
        kwargs=dict(key3=value3, key4=value4)
        ->
        args=[arg1 ,arg2]
        kwargs=dict(key1=value1, key2=value2, key3=value3, key4=value4)
        """

        if self.request.args and len(self.request.args):
            for result in self.request.args.pop(0):
                if isinstance(result, dict):
                    self.request.kwargs.update(result)

        return super().__call__(*self.request.args, **self.request.kwargs)
